import asyncio
import audioop
import itertools
import re
import unicodedata

import edge_tts
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions, tts, utils
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS
from livekit.agents.utils import codecs

_WORD_RE = re.compile(r"\w", re.UNICODE)
_SING_TAG_RE = re.compile(r"\bCANTO:\s*")

# Variación muy sutil de tono para no distorsionar el vocoder neuronal de Edge TTS
_SING_PITCH_CYCLE = itertools.cycle(["+0Hz", "+2Hz", "+0Hz", "-2Hz"])

# Umbral de amplitud PCM 16-bit bajo el cual Edge TTS emite silencio digital de relleno
_SILENCE_PEAK_THRESHOLD = 135
# Silencio máximo conservado al final de cada bloque de aire (140 ms a 24 kHz = 3360 muestras)
_MAX_TRAILING_SILENCE_SAMPLES = 3360
# Silencio máximo conservado al inicio de cada bloque de aire (20 ms a 24 kHz = 480 muestras)
_MAX_LEADING_SILENCE_SAMPLES = 480

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

_ALL_FEMALE_VOICES = {pair[1] for pair in SPANISH_VOICES.values()} | {"es-ES-ElviraNeural"}

_ACCENT_TRIGGER_RE = re.compile(
    r"\b(?:"
    r"acento|voz|ento(?:na|nacion)|"
    r"habl(?:a|ame|ar|es)\s+(?:como|en|con)|"
    r"cambi(?:a|ar|ate|es)\s+(?:a|de|tu)|"
    r"pon(?:te|me|er)?\s+(?:voz|acento|como)"
    r")\b",
    re.IGNORECASE,
)

# Fin de oración real (. ! ?) seguido de espacio, excluyendo puntos suspensivos seguidos de minúscula
_SENTENCE_BOUNDARY_RE = re.compile(r"([.!?]+)\s+")
_CLAUSE_BOUNDARY_RE = re.compile(r"(?:[,;:]|\.\.\.|—)\s+")


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
    if any(w in norm for w in ("mujer", "femenin", "chica", "ella", "nina", "senora")) or (
        re.search(r"\b\w+a\b", norm)
        and any(
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


def extract_breath_segment(buffer: str, *, is_first: bool) -> tuple[str, str] | None:
    """
    Extrae un bloque de respiración natural para síntesis neuronal:
    - El primer bloque (`is_first=True`) sale rápido (42-85 caracteres) para baja latencia,
      sin cortar muletillas iniciales ni puntos suspensivos a mitad de frase (`Mmm... mira`).
    - Los bloques siguientes agrupan 2-3 frases (140-230 caracteres) para que el modelo
      neuronal mantenga entonación continua y respiración orgánica entre oraciones.
    - Los versos con `CANTO:` se emiten verso a verso para conservar la cadencia musical.
    """
    if "CANTO:" in buffer:
        canto_idx = buffer.find("CANTO:")
        if canto_idx > 0:
            prefix = buffer[:canto_idx].strip()
            if prefix:
                return prefix, buffer[canto_idx:]
        after_canto = buffer[canto_idx + 6 :]
        m = re.search(r"[.!?\n]\s+", after_canto)
        if m:
            split_pos = canto_idx + 6 + m.end()
            return buffer[:split_pos].strip(), buffer[split_pos:]
        return None

    min_len = 42 if is_first else 140
    clause_fallback_len = 85 if is_first else 225

    if len(buffer) < min_len:
        return None

    best_sentence_split: int | None = None
    for match in _SENTENCE_BOUNDARY_RE.finditer(buffer):
        end_pos = match.end()
        if end_pos < min_len:
            continue
        punct = match.group(1)
        # Si son puntos suspensivos ("...") seguidos de minúscula, es una pausa pensativa dentro de la misma frase
        if len(punct) >= 2 and end_pos < len(buffer) and buffer[end_pos].islower():
            if end_pos < clause_fallback_len:
                continue
        best_sentence_split = end_pos
        if is_first or end_pos >= min_len:
            break

    if best_sentence_split is not None:
        return buffer[:best_sentence_split].strip(), buffer[best_sentence_split:]

    if len(buffer) >= clause_fallback_len:
        last_clause_split: int | None = None
        for match in _CLAUSE_BOUNDARY_RE.finditer(buffer):
            if min_len <= match.end() <= clause_fallback_len + 30:
                last_clause_split = match.end()
        if last_clause_split is not None:
            return buffer[:last_clause_split].strip(), buffer[last_clause_split:]

    if len(buffer) >= clause_fallback_len + 55:
        space_pos = buffer.rfind(" ", min_len, clause_fallback_len + 55)
        if space_pos != -1:
            return buffer[:space_pos].strip(), buffer[space_pos + 1 :]

    return None


def trim_segment_silence(frames: list[rtc.AudioFrame]) -> list[rtc.AudioFrame]:
    """
    Elimina el relleno de silencio digital que Edge TTS agrega al inicio (~185 ms) y al final (~900 ms)
    de cada archivo MP3, conservando intactas todas las pausas internas de respiración (comas, `...`)
    y dejando una pausa natural humana de ~140 ms entre bloques.
    """
    if not frames:
        return frames

    first_voice_idx = 0
    for idx, frame in enumerate(frames):
        peak = audioop.max(bytes(frame.data), 2) if len(frame.data) >= 2 else 0
        if peak >= _SILENCE_PEAK_THRESHOLD:
            first_voice_idx = idx
            break
    else:
        return frames[:1]

    # Conservar hasta ~20 ms antes del ataque de voz para no recortar consonantes suaves
    lead_samples = 0
    start_idx = first_voice_idx
    while start_idx > 0 and lead_samples + frames[start_idx - 1].samples_per_channel <= _MAX_LEADING_SILENCE_SAMPLES:
        start_idx -= 1
        lead_samples += frames[start_idx].samples_per_channel

    last_voice_idx = first_voice_idx
    for idx in range(len(frames) - 1, first_voice_idx - 1, -1):
        peak = audioop.max(bytes(frames[idx].data), 2) if len(frames[idx].data) >= 2 else 0
        if peak >= _SILENCE_PEAK_THRESHOLD:
            last_voice_idx = idx
            break

    # Conservar hasta ~140 ms de caída/respiración natural al final del bloque
    trail_samples = 0
    end_idx = last_voice_idx + 1
    while end_idx < len(frames) and trail_samples + frames[end_idx].samples_per_channel <= _MAX_TRAILING_SILENCE_SAMPLES:
        trail_samples += frames[end_idx].samples_per_channel
        end_idx += 1

    return frames[start_idx:end_idx]


async def _synthesize_trimmed_frames(
    edge_tts_plugin: "EdgeTTS",
    raw_text: str,
) -> list[rtc.AudioFrame]:
    """Sintetiza un bloque de texto en Edge TTS, decodifica a PCM 24 kHz y recorta silencios muertos."""
    is_singing, spoken_text = parse_sing_utterance(raw_text)
    alpha_only = re.sub(r"[^\w]", "", spoken_text, flags=re.UNICODE)
    if len(alpha_only) <= 1:
        return []

    rate = "+0%" if is_singing else edge_tts_plugin._rate
    pitch = next(_SING_PITCH_CYCLE) if is_singing else edge_tts_plugin._pitch

    decoder = codecs.AudioStreamDecoder(
        sample_rate=24000,
        num_channels=1,
        format="audio/mpeg",
    )
    frames: list[rtc.AudioFrame] = []

    async def _collect_decoded() -> None:
        async for frame in decoder:
            frames.append(frame)

    collect_task = asyncio.create_task(_collect_decoded())
    try:
        communicate = edge_tts.Communicate(
            spoken_text,
            voice=edge_tts_plugin._voice,
            rate=rate,
            volume=edge_tts_plugin._volume,
            pitch=pitch,
        )
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if data:
                    decoder.push(data)
    except edge_tts.exceptions.NoAudioReceived:
        pass
    except Exception as exc:
        decoder.end_input()
        await decoder.aclose()
        raise APIConnectionError(f"edge-tts: {exc}") from exc
    finally:
        decoder.end_input()
        await collect_task
        await decoder.aclose()

    return trim_segment_silence(frames)


class EdgeTTS(tts.TTS):
    """
    Adaptador TTS neuronal basado en Microsoft Edge Read Aloud (24 kHz mono)
    con agrupación por bloques de respiración humana, prefetch paralelo y recorte de silencios muertos.
    """

    def __init__(
        self,
        *,
        voice: str = "es-CL-LorenzoNeural",
        rate: str = "+0%",
        volume: str = "+0%",
        pitch: str = "+0Hz",
    ) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
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

    def stream(
        self,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> "EdgeSynthesizeStream":
        return EdgeSynthesizeStream(tts=self, conn_options=conn_options)


class EdgeChunkedStream(tts.ChunkedStream):
    """Flujo puntual para síntesis de un bloque único."""

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
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
        )
        frames = await _synthesize_trimmed_frames(self._edge_tts, self.input_text)
        for frame in frames:
            output_emitter.push_frame(frame)
        output_emitter.flush()


class EdgeSynthesizeStream(tts.SynthesizeStream):
    """
    Flujo continuo con agrupación de respiración humana y prefetch en paralelo:
    mientras suena el primer bloque, los bloques siguientes ya se están sintetizando y
    decodificando en segundo plano para eliminar pausas robóticas entre oraciones.
    """

    def __init__(
        self,
        *,
        tts: EdgeTTS,
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(tts=tts, conn_options=conn_options)
        self._edge_tts = tts

    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=24000,
            num_channels=1,
            mime_type="audio/pcm",
            stream=True,
        )
        output_emitter.start_segment(segment_id=utils.shortuuid())

        prefetch_sem = asyncio.Semaphore(2)
        task_queue: asyncio.Queue[asyncio.Task[list[rtc.AudioFrame]] | None] = asyncio.Queue()
        active_tasks: list[asyncio.Task[list[rtc.AudioFrame]]] = []

        async def _guarded_synth(segment_text: str) -> list[rtc.AudioFrame]:
            async with prefetch_sem:
                return await _synthesize_trimmed_frames(self._edge_tts, segment_text)

        def _enqueue_segment(segment_text: str) -> None:
            cleaned = segment_text.strip()
            if not _WORD_RE.search(cleaned):
                return
            task = asyncio.create_task(_guarded_synth(cleaned))
            active_tasks.append(task)
            task_queue.put_nowait(task)

        async def _segment_input() -> None:
            buf = ""
            is_first = True
            async for item in self._input_ch:
                if isinstance(item, self._FlushSentinel):
                    if buf.strip():
                        _enqueue_segment(buf)
                        buf = ""
                        is_first = False
                    continue

                buf += item
                while True:
                    extracted = extract_breath_segment(buf, is_first=is_first)
                    if extracted is None:
                        break
                    seg_text, buf = extracted
                    _enqueue_segment(seg_text)
                    is_first = False

            if buf.strip():
                _enqueue_segment(buf)
            task_queue.put_nowait(None)

        async def _emit_audio() -> None:
            pushed_any = False
            while True:
                task = await task_queue.get()
                if task is None:
                    break
                frames = await task
                if not frames:
                    continue
                if not pushed_any:
                    self._mark_started()
                    pushed_any = True
                for frame in frames:
                    output_emitter.push_frame(frame)

            if not pushed_any and self._pushed_text.strip():
                self._mark_started()
                silent_frame = rtc.AudioFrame(
                    data=bytes(480 * 2),
                    sample_rate=24000,
                    num_channels=1,
                    samples_per_channel=480,
                )
                output_emitter.push_frame(silent_frame)

            output_emitter.flush()

        producer = asyncio.create_task(_segment_input())
        consumer = asyncio.create_task(_emit_audio())
        try:
            await asyncio.gather(producer, consumer)
        finally:
            for t in active_tasks:
                if not t.done():
                    t.cancel()
            await utils.aio.cancel_and_wait(producer, consumer)
