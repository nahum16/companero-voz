import itertools
import re
import unicodedata

import edge_tts
from livekit.agents import APIConnectionError, APIConnectOptions, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS

_WORD_RE = re.compile(r"\w", re.UNICODE)
_SING_TAG_RE = re.compile(r"\bCANTO:\s*")

# Variación muy sutil de tono para no distorsionar el vocoder neuronal de Edge TTS
_SING_PITCH_CYCLE = itertools.cycle(["+0Hz", "+2Hz", "+0Hz", "-2Hz"])

# Catálogo real de voces neuronales en español de edge-tts: (masculina, femenina)
SPANISH_VOICES: dict[str, tuple[str, str]] = {
    "argentina": ("es-AR-TomasNeural", "es-AR-ElenaNeural"),
    "bolivia": ("es-BO-MarceloNeural", "es-BO-SofiaNeural"),
    "chile": ("es-CL-LorenzoNeural", "es-CL-CatalinaNeural"),
    "colombia": ("es-CO-GonzaloNeural", "es-CO-SalomeNeural"),
    "costa rica": ("es-CR-JuanNeural", "es-CR-MariaNeural"),
    "cuba": ("es-CU-ManuelNeural", "es-CU-BelkysNeural"),
    "dominicana": ("es-DO-EmilioNeural", "es-DO-RamonaNeural"),
    "ecuador": ("es-EC-LuisNeural", "es-EC-AndreaNeural"),
    "el salvador": ("es-SV-RodrigoNeural", "es-SV-LorenaNeural"),
    "espana": ("es-ES-AlvaroNeural", "es-ES-ElviraNeural"),
    "estados unidos": ("es-US-AlonsoNeural", "es-US-PalomaNeural"),
    "guatemala": ("es-GT-AndresNeural", "es-GT-MartaNeural"),
    "honduras": ("es-HN-CarlosNeural", "es-HN-KarlaNeural"),
    "mexico": ("es-MX-JorgeNeural", "es-MX-DaliaNeural"),
    "nicaragua": ("es-NI-FedericoNeural", "es-NI-YolandaNeural"),
    "panama": ("es-PA-RobertoNeural", "es-PA-MargaritaNeural"),
    "paraguay": ("es-PY-MarioNeural", "es-PY-TaniaNeural"),
    "peru": ("es-PE-AlexNeural", "es-PE-CamilaNeural"),
    "puerto rico": ("es-PR-VictorNeural", "es-PR-KarinaNeural"),
    "uruguay": ("es-UY-MateoNeural", "es-UY-ValentinaNeural"),
    "venezuela": ("es-VE-SebastianNeural", "es-VE-PaolaNeural"),
}

_COUNTRY_ALIASES: dict[str, str] = {
    "argentin": "argentina",
    "che": "argentina",
    "porten": "argentina",
    "bolivian": "bolivia",
    "chilen": "chile",
    "colombian": "colombia",
    "parc": "colombia",
    "paisa": "colombia",
    "costarric": "costa rica",
    "tico": "costa rica",
    "tica": "costa rica",
    "cuban": "cuba",
    "dominican": "dominicana",
    "ecuatorian": "ecuador",
    "salvadoren": "el salvador",
    "espanol": "espana",
    "castellan": "espana",
    "madrid": "espana",
    "gring": "estados unidos",
    "eeuu": "estados unidos",
    "latino": "estados unidos",
    "guatemal": "guatemala",
    "honduren": "honduras",
    "mexic": "mexico",
    "chilang": "mexico",
    "nicarag": "nicaragua",
    "panamen": "panama",
    "paraguay": "paraguay",
    "peruan": "peru",
    "boricua": "puerto rico",
    "puertorriqu": "puerto rico",
    "uruguay": "uruguay",
    "venezolan": "venezuela",
    "chamo": "venezuela",
    "caracas": "venezuela",
}

_ALL_FEMALE_VOICES = {pair[1] for pair in SPANISH_VOICES.values()}

_ACCENT_TRIGGER_RE = re.compile(
    r"\b(?:"
    r"acento|voz|ento(?:na|nacion)|"
    r"habl(?:a|ame|ar|es)\s+(?:como|en|con)|"
    r"cambi(?:a|ar|ate|es)\s+(?:a|de|tu)|"
    r"pon(?:te|me|er)?\s+(?:voz|acento|como)"
    r")\b",
    re.IGNORECASE,
)


def _strip_accents(text: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFD", text.lower())
        if unicodedata.category(ch) != "Mn"
    )


def resolve_spanish_voice(query: str, current_voice: str = "es-CL-LorenzoNeural") -> tuple[str, str]:
    """Resuelve un pedido en lenguaje natural (ej. 'argentino', 'mujer colombiana') a una voz real de edge-tts."""
    norm = _strip_accents(query)

    want_female: bool | None = None
    if any(w in norm for w in ("mujer", "femenin", "chica", "ella", "nina", "senora")) or re.search(
        r"\b\w+a\b", norm
    ) and any(
        norm.endswith(s)
        for s in (
            "mexicana",
            "argentina",
            "colombiana",
            "chilena",
            "espanola",
            "venezolana",
            "peruana",
            "cubana",
            "dominicana",
            "uruguaya",
            "ecuatoriana",
            "boliviana",
            "paraguaya",
            "panamena",
        )
    ):
        want_female = True
    elif any(w in norm for w in ("hombre", "masculin", "chico", "varon", "el")):
        want_female = False

    if want_female is None:
        want_female = current_voice in _ALL_FEMALE_VOICES

    country: str | None = None
    for key in SPANISH_VOICES:
        if key in norm:
            country = key
            break
    if country is None:
        for alias, target in _COUNTRY_ALIASES.items():
            if alias in norm:
                country = target
                break

    if country is None:
        # Si sólo pidió cambiar de género (ej. "voz de mujer"), conservar el país actual
        for key, pair in SPANISH_VOICES.items():
            if current_voice in pair:
                country = key
                break
        if country is None:
            country = "chile"

    male_v, female_v = SPANISH_VOICES[country]
    chosen = female_v if want_female else male_v
    gender_label = "femenina" if want_female else "masculina"
    return chosen, f"{country} ({gender_label})"


def detect_accent_change_request(
    user_text: str,
    current_voice: str = "es-CL-LorenzoNeural",
) -> tuple[str, str] | None:
    """
    Si el texto del usuario pide cambiar de acento o de voz, devuelve (nueva_voz, etiqueta);
    en caso contrario devuelve None.
    """
    norm = _strip_accents(user_text)
    if not _ACCENT_TRIGGER_RE.search(norm):
        return None

    has_country = any(k in norm for k in SPANISH_VOICES) or any(
        a in norm for a in _COUNTRY_ALIASES
    )
    has_gender = any(
        w in norm
        for w in ("mujer", "femenin", "chica", "hombre", "masculin", "chico", "varon")
    )
    if not has_country and not has_gender:
        return None

    return resolve_spanish_voice(user_text, current_voice)


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
    def voice(self) -> str:
        return self._voice

    def set_voice(self, voice: str) -> None:
        self._voice = voice

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

        rate = "+0%" if is_singing else self._edge_tts._rate
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
