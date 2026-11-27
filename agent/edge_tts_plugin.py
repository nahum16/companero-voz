import itertools
import re

import edge_tts
from livekit.agents import APIConnectionError, APIConnectOptions, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

_WORD_RE = re.compile(r"\w", re.UNICODE)
_SING_TAG_RE = re.compile(r"\bCANTO:\s*")

# Progresión de tonos (pitch) para dar entonación melódica verso a verso en modo canto
_SING_PITCH_CYCLE = itertools.cycle(
    ["+14Hz", "+22Hz", "+8Hz", "+18Hz", "+4Hz", "-4Hz"]
)


def parse_sing_utterance(text: str) -> tuple[bool, str]:
    """Detecta la marca `CANTO:`, la elimina del texto y devuelve (es_canto, texto_limpio)."""
    if _SING_TAG_RE.search(text):
        cleaned = _SING_TAG_RE.sub("", text).strip()
        return True, cleaned
    return False, text


class EdgeTTS(tts.TTS):
    """Adaptador TTS gratuito basado en Microsoft Edge Read Aloud (MP3 24 kHz mono)."""

    def __init__(
        self,
        *,
        voice: str = "es-CL-LorenzoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=24000,
            num_channels=1,
        )
        self._voice = voice
        self._rate = rate
        self._volume = volume
        self._pitch = pitch

    @property
    def model(self) -> str:
        return "edge-tts"

    @property
    def provider(self) -> str:
        return "microsoft-edge"

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "EdgeChunkedStream":
        return EdgeChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class EdgeChunkedStream(tts.ChunkedStream):
    """Flujo por fragmentos que obtiene MP3 desde edge-tts y lo decodifica en AudioEmitter."""

    def __init__(
        self,
        *,
        tts: EdgeTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, input_text=input_text, conn_options=conn_options)
        self._edge_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        # Edge entrega MP3 a 24 kHz mono; AudioEmitter lo decodifica internamente con PyAV
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/mpeg",
        )

        is_singing, spoken_text = parse_sing_utterance(self.input_text)

        if not _WORD_RE.search(spoken_text):
            output_emitter.flush()
            return

        rate = "-10%" if is_singing else self._edge_tts._rate
        pitch = next(_SING_PITCH_CYCLE) if is_singing else self._edge_tts._pitch

        try:
            communicate = edge_tts.Communicate(
                spoken_text,
                voice=self._edge_tts._voice,
                rate=rate,
                volume=self._edge_tts._volume,
                pitch=pitch,
            )
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    data = chunk.get("data")
                    if data:
                        output_emitter.push(data)
        except edge_tts.exceptions.NoAudioReceived:
            output_emitter.flush()
            return
        except Exception as exc:
            raise APIConnectionError(f"edge-tts: {exc}") from exc

        output_emitter.flush()
