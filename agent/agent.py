import asyncio
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
    llm,
    room_io,
    tokenize,
    tts,
)
from livekit.plugins import groq, silero

from edge_tts_plugin import EdgeTTS
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


def _human_participants(room: rtc.Room) -> list[rtc.RemoteParticipant]:
    return [
        p
        for p in room.remote_participants.values()
        if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    ]


class Companion(Agent):
    """Agente conversacional optimizado para charla individual o grupal por audífonos."""

    def __init__(
        self,
        *,
        room: rtc.Room | None = None,
        multi_input: MultiParticipantAudioInput | None = None,
    ) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)
        self._room = room
        self._multi_input = multi_input
        self._turn_speakers: dict[str, str] = {}

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

        async for chunk in Agent.default.llm_node(self, trimmed, tools, model_settings):
            yield chunk

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
    # Un umbral de activación más alto (0.6 frente a 0.5) filtra ruido de viento y tráfico.
    # Un silencio mínimo de 0.6 s evita cortar pausas naturales al hablar caminando.
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
    logger.info("Agente conectado a la sala %s", ctx.room.name)

    model_name = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")
    llm_kwargs: dict[str, Any] = {
        "model": model_name,
        "temperature": 0.7,
        "max_completion_tokens": 220,
    }
    if "qwen" in model_name.lower():
        llm_kwargs["reasoning_effort"] = "none"

    edge_voice = os.getenv("EDGE_TTS_VOICE", "es-CL-LorenzoNeural")
    edge_rate = os.getenv("EDGE_TTS_RATE", "+5%")

    multi_input = MultiParticipantAudioInput(ctx.room)
    multi_input.start()

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=groq.STT(
            model="whisper-large-v3-turbo",
            language="es",
            prompt="Conversación casual en español latinoamericano.",
        ),
        llm=groq.LLM(**llm_kwargs),
        tts=tts.StreamAdapter(
            tts=EdgeTTS(voice=edge_voice, rate=edge_rate),
            sentence_tokenizer=tokenize.basic.SentenceTokenizer(min_sentence_len=12),
        ),
        turn_handling={
            "turn_detection": "vad",
            "endpointing": {
                "mode": "fixed",
                "min_delay": 0.5,
                "max_delay": 3.0,
            },
            # Desactivar `resume_false_interruption` evita que `_ParticipantAudioOutput`
            # quede en estado `paused` permanente cuando ocurren interrupciones seguidas
            # en salas con dos o más personas.
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
        # Margen de 15 s por si alguien sale un momento a compartir el enlace y vuelve
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
        agent=Companion(room=ctx.room, multi_input=multi_input),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=False,
            close_on_disconnect=False,
        ),
    )
    session.say(GREETING, allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(server)
