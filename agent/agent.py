import asyncio
import itertools
import logging
import os
import re
from collections.abc import AsyncIterable
from typing import Any

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    ModelSettings,
    StopResponse,
    cli,
    function_tool,
    llm,
    room_io,
    tokenize,
    tts,
)
from livekit.plugins import groq, silero

from edge_tts_plugin import EdgeTTS, resolve_spanish_voice
from multi_audio_input import MultiParticipantAudioInput
from prompts import GREETING, SYSTEM_PROMPT
from text_utils import speech_text_filter

load_dotenv()

logger = logging.getLogger("companero")
MAX_TURNS = 12

# Frases típicas de alucinación de Whisper ante ruido de fondo o silencios
_WHISPER_HALLUCINATION_RE = re.compile(
    r"^\s*(?:"
    r"gracias por ver(?: el video)?\.?|"
    r"subt[ií]tulos realizados por.*|"
    r"subt[ií]tulos por.*|"
    r"suscr[ií]bete(?: al canal)?\.?|"
    r"m[uú]sica\.?"
    r")\s*$",
    re.IGNORECASE,
)


def load_groq_keys() -> list[str]:
    """Obtiene la lista de claves de Groq desde GROQ_API_KEYS y GROQ_API_KEY sin duplicados."""
    raw_multi = os.getenv("GROQ_API_KEYS", "")
    raw_single = os.getenv("GROQ_API_KEY", "")
    candidates = [k.strip() for k in raw_multi.split(",") if k.strip()]
    if raw_single.strip():
        candidates.append(raw_single.strip())

    unique: list[str] = []
    for key in candidates:
        if key not in unique:
            unique.append(key)
    return unique


GROQ_KEYS = load_groq_keys()
_key_cycle = itertools.cycle(range(max(len(GROQ_KEYS), 1)))


def next_key_index() -> int:
    return next(_key_cycle)


def build_groq_llm(api_key: str, model_name: str) -> groq.LLM:
    llm_kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0.85,
        "max_completion_tokens": 1200,
    }
    if "qwen" in model_name.lower():
        llm_kwargs["reasoning_effort"] = "none"
    return groq.LLM(**llm_kwargs)


def _human_participants(room: rtc.Room) -> list[rtc.RemoteParticipant]:
    return [
        p
        for p in room.remote_participants.values()
        if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    ]


class Companion(Agent):
    """Agente conversacional con rotación automática multi-cuenta y multi-modelo en Groq."""

    def __init__(
        self,
        *,
        room: rtc.Room | None = None,
        multi_input: MultiParticipantAudioInput | None = None,
        edge_tts_plugin: EdgeTTS | None = None,
        initial_key_idx: int = 0,
    ) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)
        self._room = room
        self._multi_input = multi_input
        self._edge_tts = edge_tts_plugin
        self._turn_speakers: dict[str, str] = {}
        self._key_idx = initial_key_idx

    @function_tool
    async def cambiar_acento(self, acento_o_voz: str) -> str:
        """Cambia el acento o género de la voz del agente en tiempo real (ej. mexicano, argentino, colombiano, español, chileno, venezolano, peruano, cubano, mujer, hombre)."""
        if self._edge_tts is None:
            return "No fue posible cambiar la voz en este momento."
        new_voice, label = resolve_spanish_voice(acento_o_voz, self._edge_tts.voice)
        self._edge_tts.set_voice(new_voice)
        logger.info("Voz cambiada en vivo a %s (%s)", new_voice, label)
        return f"Voz cambiada a {label} ({new_voice}). Responde ya usando este acento."

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        text = (new_message.text_content or "").strip()
        if not text or _WHISPER_HALLUCINATION_RE.match(text):
            logger.info("Ignorando alucinación de ruido de Whisper: %r", text)
            raise StopResponse()

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | Any]:
        # Ventana deslizante de los últimos 12 turnos (24 mensajes) conservando el system prompt
        trimmed = chat_ctx.copy(
            exclude_function_call=True,
            exclude_handoff=True,
            exclude_config_update=True,
        )
        trimmed.truncate(max_items=MAX_TURNS * 2)

        speaker = self._multi_input.last_active_speaker if self._multi_input else None
        humans = _human_participants(self._room) if self._room else []
        should_tag = bool(speaker) and (len(humans) > 1 or speaker != "Usuario")

        if should_tag and speaker:
            for idx, item in enumerate(trimmed.items):
                if isinstance(item, llm.ChatMessage) and item.role == "user":
                    msg_speaker = self._turn_speakers.get(item.id)
                    if msg_speaker is None and idx == len(trimmed.items) - 1:
                        msg_speaker = speaker
                        self._turn_speakers[item.id] = speaker
                    if msg_speaker:
                        text = item.text_content or ""
                        prefix = f"[{msg_speaker}]: "
                        if text and not text.startswith(prefix):
                            trimmed.items[idx] = item.model_copy(
                                update={"content": [f"{prefix}{text}"]},
                            )

        if not GROQ_KEYS:
            async for chunk in Agent.default.llm_node(self, trimmed, tools, model_settings):
                yield chunk
            return

        primary_model = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")
        models_order = [primary_model]
        for fallback_model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
            if fallback_model not in models_order:
                models_order.append(fallback_model)

        num_keys = len(GROQ_KEYS)
        start_idx = self._key_idx % num_keys
        # Avanzar el índice para balancear turnos entre todas las cuentas disponibles
        self._key_idx = (start_idx + 1) % num_keys

        last_err: Exception | None = None
        for model_name in models_order:
            for offset in range(num_keys):
                idx = (start_idx + offset) % num_keys
                api_key = GROQ_KEYS[idx]
                emitted_any = False
                try:
                    llm_instance = build_groq_llm(api_key, model_name)
                    tool_choice = model_settings.tool_choice if model_settings else None
                    async with llm_instance.chat(
                        chat_ctx=trimmed,
                        tools=tools,
                        tool_choice=tool_choice,
                    ) as stream:
                        async for chunk in stream:
                            emitted_any = True
                            yield chunk
                    return
                except Exception as exc:
                    last_err = exc
                    if emitted_any:
                        raise
                    logger.warning(
                        "Clave Groq #%d (%s...) falló con %s (%s); rotando a la siguiente cuenta...",
                        idx + 1,
                        api_key[:10],
                        model_name,
                        exc,
                    )
                    continue

        if last_err is not None:
            raise last_err

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterable[rtc.AudioFrame]:
        async for frame in Agent.default.tts_node(
            self,
            speech_text_filter(text),
            model_settings,
        ):
            yield frame


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load(
        min_speech_duration=0.15,
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE", "0.6")),
        prefix_padding_duration=0.3,
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.6")),
        sample_rate=16000,
    )


server = AgentServer(
    setup_fnc=prewarm,
    num_idle_processes=int(os.getenv("AGENT_IDLE_PROCESSES", "1")),
    load_threshold=0.9,
    port=int(os.getenv("PORT", "8081")),
    job_memory_warn_mb=450,
)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    key_idx = next_key_index()
    active_key = GROQ_KEYS[key_idx] if GROQ_KEYS else os.getenv("GROQ_API_KEY", "")
    logger.info(
        "Agente conectado a la sala %s [pool Groq: %d cuentas, iniciando con cuenta #%d]",
        ctx.room.name,
        len(GROQ_KEYS),
        key_idx + 1,
    )

    model_name = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")
    edge_voice = os.getenv("EDGE_TTS_VOICE", "es-CL-LorenzoNeural")
    edge_rate = os.getenv("EDGE_TTS_RATE", "+5%")

    multi_input = MultiParticipantAudioInput(ctx.room)
    multi_input.start()
    edge_tts_plugin = EdgeTTS(voice=edge_voice, rate=edge_rate)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=groq.STT(
            model="whisper-large-v3-turbo",
            api_key=active_key,
            language="es",
            prompt="Conversación casual en español latinoamericano.",
        ),
        llm=build_groq_llm(active_key, model_name),
        tts=tts.StreamAdapter(
            tts=edge_tts_plugin,
            sentence_tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=12),
        ),
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {
                "mode": "fixed",
                "min_delay": 0.5,
                "max_delay": 3.0,
            },
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.75,
                "resume_false_interruption": False,
                "false_interruption_timeout": None,
            },
        },
        tts_text_transforms=["filter_markdown", "filter_emoji"],
    )

    session.input.audio = multi_input

    empty_room_task: asyncio.Task[None] | None = None

    async def _close_if_still_empty() -> None:
        await asyncio.sleep(15.0)
        if not _human_participants(ctx.room):
            logger.info(
                "Sala %s sin participantes humanos tras 15s; eliminando sala y cerrando sesión.",
                ctx.room.name,
            )
            await multi_input.aclose()
            await session.aclose()
            try:
                await ctx.delete_room(room_name=ctx.room.name)
            except Exception:
                logger.debug("No se pudo eliminar la sala al cerrar", exc_info=True)
            ctx.shutdown(reason="Sala vacía")

    def _on_participant_connected(participant: rtc.RemoteParticipant) -> None:
        nonlocal empty_room_task
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        if empty_room_task is not None:
            empty_room_task.cancel()
            empty_room_task = None
        humans = [f"{p.name or p.identity} ({p.identity})" for p in _human_participants(ctx.room)]
        logger.info("Participante entró a %s. Humanos en sala (%d): %s", ctx.room.name, len(humans), humans)

    def _on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        nonlocal empty_room_task
        humans = [f"{p.name or p.identity} ({p.identity})" for p in _human_participants(ctx.room)]
        logger.info(
            "Participante salió de %s (%s). Humanos restantes (%d): %s",
            ctx.room.name,
            participant.identity,
            len(humans),
            humans,
        )
        if not humans and empty_room_task is None:
            empty_room_task = asyncio.create_task(_close_if_still_empty())

    ctx.room.on("participant_connected", _on_participant_connected)
    ctx.room.on("participant_disconnected", _on_participant_disconnected)
    ctx.add_shutdown_callback(multi_input.aclose)

    await session.start(
        agent=Companion(
            room=ctx.room,
            multi_input=multi_input,
            edge_tts_plugin=edge_tts_plugin,
            initial_key_idx=key_idx,
        ),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=False,
            close_on_disconnect=False,
        ),
    )
    session.say(GREETING, allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(server)
