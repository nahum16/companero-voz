import asyncio
import itertools
import json
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

from edge_tts_plugin import EdgeTTS, detect_accent_change_request
from fs_gateway import execute_fs_action, is_file_action_request
from multi_audio_input import MultiParticipantAudioInput
from prompts import GREETING, SYSTEM_PROMPT
from text_utils import speech_text_filter

load_dotenv()

logger = logging.getLogger("companero")
MAX_TURNS = 12

# Frases típicas de alucinación de Whisper ante ruido de fondo o silencios
_WHISPER_HALLUCINATION_RE = re.compile(
    r"^\s*[¡!¿?\s]*(?:"
    r"gracias por ver(?: el video)?|"
    r"subt[ií]tulos realizados por.*|"
    r"subt[ií]tulos por.*|"
    r"suscr[ií]bete(?: al canal)?|"
    r"conversaci[oó]n casual en espa[ñn]ol(?: latinoamericano)?|"
    r"m[uú]sica"
    r")[.¡!¿?\s]*$",
    re.IGNORECASE,
)


def _extract_chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    delta = getattr(chunk, "delta", None)
    if delta is not None:
        return getattr(delta, "content", None) or ""
    return ""


def _chunk_has_tool_call(chunk: Any) -> bool:
    delta = getattr(chunk, "delta", None)
    return bool(delta is not None and getattr(delta, "tool_calls", None))


def is_stub_assistant_text(text: str) -> bool:
    """Detecta respuestas truncadas por interrupción temprana ('M', 'J', 'De', 'Ay,', 'Uf, por')."""
    cleaned = text.strip()
    if len(cleaned) < 12:
        # Permitir sólo respuestas cortas completas válidas con puntuación final (ej. '¡Claro!', 'Vale.')
        words = re.findall(r"\w+", cleaned, re.UNICODE)
        if not words or len(words[0]) <= 2 and len(words) == 1:
            return True
        if cleaned[-1] not in ".!?¡¿":
            return True
    return False


def sanitize_chat_context(chat_ctx: llm.ChatContext, max_turns: int = MAX_TURNS) -> llm.ChatContext:
    """
    Limpia el historial antes de enviarlo al LLM:
    1. Elimina fragmentos de 1 letra o frases truncadas del asistente ('M', 'J', 'Ay,', 'Uf, por')
       que envenenan el contexto few-shot de Qwen.
    2. Convierte salidas de herramientas (FunctionCallOutput de api.php) en notas de texto plano
       dentro del turno del usuario para evitar bucles de tool_call en Groq.
    3. Fusiona mensajes consecutivos de rol 'user' en un solo turno para mantener la
       alternancia estricta user <-> assistant que exige la plantilla ChatML.
    """
    raw_copy = chat_ctx.copy(
        exclude_handoff=True,
        exclude_config_update=True,
    )

    cleaned_items: list[Any] = []
    for item in raw_copy.items:
        if isinstance(item, llm.FunctionCall):
            continue

        if isinstance(item, llm.FunctionCallOutput):
            note = f"\n[Resultado ejecutado en api.php ({item.name}): {item.output}]"
            if (
                cleaned_items
                and isinstance(cleaned_items[-1], llm.ChatMessage)
                and cleaned_items[-1].role == "user"
            ):
                prev_msg = cleaned_items[-1]
                prev_text = (prev_msg.text_content or "").strip()
                cleaned_items[-1] = prev_msg.model_copy(update={"content": [f"{prev_text}{note}"]})
            else:
                cleaned_items.append(llm.ChatMessage(role="user", content=[note.strip()]))
            continue

        if not isinstance(item, llm.ChatMessage):
            cleaned_items.append(item)
            continue

        if item.role in ("system", "developer"):
            cleaned_items.append(item)
            continue

        text = (item.text_content or "").strip()
        if not text:
            continue

        if item.role == "assistant":
            if is_stub_assistant_text(text):
                continue
            cleaned_items.append(item)
            continue

        if item.role == "user":
            if _WHISPER_HALLUCINATION_RE.match(text):
                continue
            if (
                cleaned_items
                and isinstance(cleaned_items[-1], llm.ChatMessage)
                and cleaned_items[-1].role == "user"
            ):
                prev_msg = cleaned_items[-1]
                prev_text = (prev_msg.text_content or "").strip()
                merged_text = f"{prev_text} {text}".strip() if prev_text else text
                cleaned_items[-1] = prev_msg.model_copy(update={"content": [merged_text]})
            else:
                cleaned_items.append(item)

    raw_copy.items[:] = cleaned_items
    raw_copy.truncate(max_items=max_turns * 2)
    return raw_copy


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
    # Nota: el límite gratuito de Output Tokens Per Minute (OTPM) de qwen/qwen3.8-27b
    # en Groq es de 1.000 tokens/min por cuenta; 450 tokens (~350 palabras) permite
    # respuestas largas sin superar el tope OTPM en una sola llamada.
    llm_kwargs: dict[str, Any] = {
        "model": model_name,
        "api_key": api_key,
        "temperature": 0.78,
        "max_completion_tokens": 450,
    }
    lower_model = model_name.lower()
    if "qwen" in lower_model:
        llm_kwargs["reasoning_effort"] = "none"
    elif "gpt-oss" in lower_model:
        llm_kwargs["reasoning_effort"] = "low"
    return groq.LLM(**llm_kwargs)


def _human_participants(room: rtc.Room) -> list[rtc.RemoteParticipant]:
    return [
        p
        for p in room.remote_participants.values()
        if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
    ]


class Companion(Agent):
    """Agente conversacional con rotación automática multi-cuenta y cambio de acento en vivo."""

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

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        text = (new_message.text_content or "").strip()
        if not text or _WHISPER_HALLUCINATION_RE.match(text):
            logger.info("Ignorando alucinación de ruido de Whisper: %r", text)
            raise StopResponse()

        if self._edge_tts is not None:
            accent_match = detect_accent_change_request(text, self._edge_tts.voice)
            if accent_match is not None:
                new_voice, label = accent_match
                self._edge_tts.set_voice(new_voice)
                logger.info("Voz cambiada en vivo a %s (%s)", new_voice, label)

    @function_tool(
        name="gestionar_archivos",
        description=(
            "Ejecuta acciones reales sobre los archivos y carpetas del proyecto usando api.php: "
            "accion puede ser 'listar', 'arbol', 'leer', 'crear', 'escribir', 'parchear', "
            "'buscar', 'copiar', 'mover', 'eliminar' o 'info'."
        ),
    )
    async def gestionar_archivos(
        self,
        accion: str,
        ruta: str = "",
        contenido: str = "",
        tipo: str = "archivo",
        texto: str = "",
        extension: str = "",
        origen: str = "",
        destino: str = "",
        linea_inicio: int = 0,
        linea_fin: int = 0,
        nuevo_codigo: str = "",
    ) -> str:
        logger.info("Ejecutando api.php -> accion=%s ruta=%r", accion, ruta or origen)
        result = await asyncio.to_thread(
            execute_fs_action,
            accion,
            ruta=ruta,
            contenido=contenido,
            tipo=tipo,
            texto=texto,
            extension=extension,
            origen=origen,
            destino=destino,
            linea_inicio=linea_inicio,
            linea_fin=linea_fin,
            nuevo_codigo=nuevo_codigo,
        )
        return json.dumps(result, ensure_ascii=False)

    async def llm_node(
        self,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[llm.ChatChunk | str | Any]:
        has_trailing_tool_output = bool(
            chat_ctx.items and isinstance(chat_ctx.items[-1], llm.FunctionCallOutput)
        )

        raw_last_user_text = ""
        for item in reversed(chat_ctx.items):
            if isinstance(item, llm.ChatMessage) and item.role == "user":
                raw_last_user_text = (item.text_content or "").strip()
                break

        enable_fs_tool = (not has_trailing_tool_output) and is_file_action_request(raw_last_user_text)
        active_tools = tools if enable_fs_tool else []

        speaker = self._multi_input.last_active_speaker if self._multi_input else None
        humans = _human_participants(self._room) if self._room else []
        should_tag = bool(speaker) and (len(humans) > 1 or speaker != "Usuario")

        working_ctx = chat_ctx.copy(
            exclude_handoff=True,
            exclude_config_update=True,
        )

        if should_tag and speaker:
            for idx, item in enumerate(working_ctx.items):
                if isinstance(item, llm.ChatMessage) and item.role == "user":
                    msg_speaker = self._turn_speakers.get(item.id)
                    if msg_speaker is None and idx == len(working_ctx.items) - 1:
                        msg_speaker = speaker
                        self._turn_speakers[item.id] = speaker
                    if msg_speaker and msg_speaker != "Usuario":
                        text = item.text_content or ""
                        prefix = f"[{msg_speaker}]: "
                        if text and not text.startswith(prefix):
                            working_ctx.items[idx] = item.model_copy(
                                update={"content": [f"{prefix}{text}"]},
                            )

        trimmed = sanitize_chat_context(working_ctx, max_turns=MAX_TURNS)

        if not GROQ_KEYS:
            async for chunk in Agent.default.llm_node(self, trimmed, active_tools, model_settings):
                yield chunk
            return

        primary_model = os.getenv("GROQ_LLM_MODEL", "qwen/qwen3.8-27b")
        models_order = [primary_model]
        for fallback_model in ("openai/gpt-oss-120b", "openai/gpt-oss-20b"):
            if fallback_model not in models_order:
                models_order.append(fallback_model)

        num_keys = len(GROQ_KEYS)
        start_idx = self._key_idx % num_keys
        self._key_idx = (start_idx + 1) % num_keys

        last_err: Exception | None = None
        for model_name in models_order:
            for offset in range(num_keys):
                idx = (start_idx + offset) % num_keys
                api_key = GROQ_KEYS[idx]
                flushed_to_caller = False
                initial_chunks: list[Any] = []
                accumulated_text = ""
                try:
                    llm_instance = build_groq_llm(api_key, model_name)
                    tool_choice = (
                        model_settings.tool_choice
                        if (model_settings and active_tools)
                        else None
                    )
                    async with llm_instance.chat(
                        chat_ctx=trimmed,
                        tools=active_tools,
                        tool_choice=tool_choice,
                    ) as stream:
                        async for chunk in stream:
                            if not flushed_to_caller:
                                initial_chunks.append(chunk)
                                accumulated_text += _extract_chunk_text(chunk)
                                if len(accumulated_text.strip()) >= 4 or _chunk_has_tool_call(chunk):
                                    flushed_to_caller = True
                                    for pending in initial_chunks:
                                        yield pending
                                    initial_chunks.clear()
                            else:
                                yield chunk

                    if not flushed_to_caller:
                        logger.warning(
                            "Modelo %s (clave #%d) devolvió respuesta vacía o truncada (%r); probando siguiente modelo...",
                            model_name,
                            idx + 1,
                            accumulated_text,
                        )
                        break
                    return
                except Exception as exc:
                    last_err = exc
                    if flushed_to_caller:
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
        min_speech_duration=0.22,
        min_silence_duration=float(os.getenv("VAD_MIN_SILENCE", "0.60")),
        prefix_padding_duration=0.3,
        activation_threshold=float(os.getenv("VAD_ACTIVATION_THRESHOLD", "0.55")),
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
    edge_rate = os.getenv("EDGE_TTS_RATE", "+0%")

    multi_input = MultiParticipantAudioInput(ctx.room)
    multi_input.start()
    edge_tts_plugin = EdgeTTS(voice=edge_voice, rate=edge_rate)

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=groq.STT(
            model="whisper-large-v3-turbo",
            api_key=active_key,
            language="es",
        ),
        llm=build_groq_llm(active_key, model_name),
        tts=edge_tts_plugin,
        turn_handling={
            "turn_detection": "vad",
            "preemptive_generation": {
                "enabled": False,
            },
            "endpointing": {
                "mode": "fixed",
                "min_delay": 0.50,
                "max_delay": 2.5,
            },
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.85,
                "resume_false_interruption": False,
                "false_interruption_timeout": None,
            },
        },
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
