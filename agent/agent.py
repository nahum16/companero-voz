import logging
import os
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
    cli,
    llm,
    tokenize,
    tts,
)
from livekit.plugins import groq, silero

from edge_tts_plugin import EdgeTTS
from prompts import GREETING, SYSTEM_PROMPT
from text_utils import speech_text_filter

load_dotenv()

logger = logging.getLogger("companero")
MAX_TURNS = 12


class Companion(Agent):
    """Agente conversacional optimizado para charla fluida por audífonos."""

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT)

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
    port=int(os.getenv("PORT", "8081")),
    job_memory_warn_mb=450,
)


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    logger.info("Agente conectado a la sala %s", ctx.room.name)

    model_name = os.getenv("GROQ_LLM_MODEL", "llama-3.3-70b-versatile")
    llm_kwargs: dict[str, Any] = {
        "model": model_name,
        "temperature": 0.7,
        "max_completion_tokens": 220,
    }
    if "qwen" in model_name.lower():
        llm_kwargs["reasoning_effort"] = "none"

    edge_voice = os.getenv("EDGE_TTS_VOICE", "es-CL-LorenzoNeural")
    edge_rate = os.getenv("EDGE_TTS_RATE", "+5%")

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
            "interruption": {
                "enabled": True,
                "mode": "vad",
                "min_duration": 0.6,
                "resume_false_interruption": True,
                "false_interruption_timeout": 1.5,
            },
        },
        tts_text_transforms=["filter_markdown", "filter_emoji"],
    )

    await session.start(agent=Companion(), room=ctx.room)
    session.say(GREETING, allow_interruptions=True)


if __name__ == "__main__":
    cli.run_app(server)
