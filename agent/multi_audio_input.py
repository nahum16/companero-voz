import asyncio
import collections
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import numpy as np
from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice import io as voice_io

logger = logging.getLogger("companero.multi_audio")

# Umbral RMS (~-43 dBFS en PCM 16 bits) por encima del cual consideramos que un
# micrófono tiene voz activa, y ventana de cola (hangover) para no cortar finales de frase
# ni impedir que Silero VAD detecte el silencio de fin de turno cuando hay 2+ micrófonos abiertos.
SPEECH_RMS_THRESHOLD = 220.0
SPEECH_HANGOVER_SEC = 0.65


def mix_int16_frames(frames: list[rtc.AudioFrame]) -> rtc.AudioFrame:
    """Mezcla N frames PCM de 16 bits sumando muestras y recortando a [-32768, 32767]."""
    if len(frames) == 1:
        return frames[0]

    base = frames[0]
    target_len = base.samples_per_channel * base.num_channels
    acc = np.zeros(target_len, dtype=np.int32)

    for frame in frames:
        arr = np.frombuffer(frame.data, dtype=np.int16)
        if arr.size == target_len:
            acc += arr
        elif arr.size > target_len:
            acc += arr[:target_len]
        else:
            acc[: arr.size] += arr

    mixed = np.clip(acc, -32768, 32767).astype(np.int16)
    return rtc.AudioFrame(
        data=mixed.tobytes(),
        sample_rate=base.sample_rate,
        num_channels=base.num_channels,
        samples_per_channel=base.samples_per_channel,
    )


def make_silence_frame(base: rtc.AudioFrame) -> rtc.AudioFrame:
    """Genera un frame de silencio con el mismo formato y duración que `base`."""
    num_samples = base.samples_per_channel * base.num_channels
    return rtc.AudioFrame(
        data=bytes(num_samples * 2),
        sample_rate=base.sample_rate,
        num_channels=base.num_channels,
        samples_per_channel=base.samples_per_channel,
    )


def compute_rms_int16(frame: rtc.AudioFrame) -> float:
    """Calcula la energía RMS de un frame PCM de 16 bits."""
    samples = np.frombuffer(frame.data, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


@dataclass
class _TrackState:
    track_sid: str
    participant_identity: str
    display_name: str
    task: asyncio.Task[None]
    buffer: collections.deque[rtc.AudioFrame] = field(
        default_factory=lambda: collections.deque(maxlen=4),
    )
    preroll: collections.deque[rtc.AudioFrame] = field(
        default_factory=lambda: collections.deque(maxlen=3),
    )
    last_frame_ts: float = 0.0
    speaking_until: float = 0.0
    was_speaking: bool = False


class MultiParticipantAudioInput(voice_io.AudioInput):
    """
    Entrada de audio multipersona sincronizada por reloj único (50 ms = 20 frames/s).

    - Una pista activa actúa como reloj maestro (con failover automático en 90 ms si se
      silencia o desconecta), garantizando que AgentSession siempre reciba frames a velocidad
      exacta 1x sin duplicar ni atrasar el flujo cuando hay 2 o más personas en la sala.
    - Puerta de voz por participante (voice-gating con pre-roll y hangover de 0.65 s):
      solo se mezclan las pistas que tienen voz activa, evitando que el ruido de fondo
      del micrófono abierto de una segunda persona impida a Silero VAD detectar el silencio
      de fin de turno cuando la primera persona termina de hablar.
    """

    def __init__(
        self,
        room: rtc.Room,
        *,
        sample_rate: int = 24000,
        num_channels: int = 1,
        frame_size_ms: int = 50,
    ) -> None:
        super().__init__(label="MultiParticipantRoomIO")
        self._room = room
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._frame_size_ms = frame_size_ms

        self._attached = True
        self._closed = False
        self._started = False

        self._out_ch = utils.aio.Chan[rtc.AudioFrame]()
        self._tracks: dict[str, _TrackState] = {}
        self._primary_sid: str | None = None
        self._last_primary_ts: float = 0.0

        self.last_active_speaker: str | None = None
        self._active_speaker_rms = 0.0
        self._active_speaker_ts = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("track_unsubscribed", self._on_track_unsubscribed)
        self._room.on("track_unpublished", self._on_track_unpublished)
        self._room.on("participant_disconnected", self._on_participant_disconnected)

        for participant in self._room.remote_participants.values():
            if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                continue
            for pub in participant.track_publications.values():
                if (
                    pub.track is not None
                    and pub.source == rtc.TrackSource.SOURCE_MICROPHONE
                    and pub.track.kind == rtc.TrackKind.KIND_AUDIO
                ):
                    self._start_track_stream(pub.track, participant)

    def on_attached(self) -> None:
        self._attached = True

    def on_detached(self) -> None:
        self._attached = False

    def __aiter__(self) -> AsyncIterator[rtc.AudioFrame]:
        return self

    async def __anext__(self) -> rtc.AudioFrame:
        return await self._out_ch.__anext__()

    def _on_track_subscribed(
        self,
        track: rtc.RemoteTrack,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if self._closed:
            return
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            return
        if (
            publication.source != rtc.TrackSource.SOURCE_MICROPHONE
            or track.kind != rtc.TrackKind.KIND_AUDIO
        ):
            return
        self._start_track_stream(track, participant)

    def _on_track_unsubscribed(
        self,
        track: rtc.RemoteTrack,
        _publication: rtc.RemoteTrackPublication,
        _participant: rtc.RemoteParticipant,
    ) -> None:
        self._stop_track_stream(track.sid)

    def _on_track_unpublished(
        self,
        publication: rtc.RemoteTrackPublication,
        _participant: rtc.RemoteParticipant,
    ) -> None:
        self._stop_track_stream(publication.sid)

    def _on_participant_disconnected(
        self,
        participant: rtc.RemoteParticipant,
    ) -> None:
        sids = [
            sid
            for sid, st in self._tracks.items()
            if st.participant_identity == participant.identity
        ]
        for sid in sids:
            self._stop_track_stream(sid)

    def _start_track_stream(
        self,
        track: rtc.RemoteTrack,
        participant: rtc.RemoteParticipant,
    ) -> None:
        track_sid = track.sid
        if track_sid in self._tracks:
            return

        display_name = (participant.name or "").strip() or participant.identity
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            frame_size_ms=self._frame_size_ms,
        )
        task = asyncio.create_task(
            self._read_track_loop(track_sid, participant.identity, display_name, stream),
        )
        self._tracks[track_sid] = _TrackState(
            track_sid=track_sid,
            participant_identity=participant.identity,
            display_name=display_name,
            task=task,
        )
        if self._primary_sid is None:
            self._primary_sid = track_sid
        logger.info(
            "Escuchando micrófono de %s (%s) [total micrófonos en sala: %d]",
            display_name,
            participant.identity,
            len(self._tracks),
        )

    def _stop_track_stream(self, track_sid: str) -> None:
        state = self._tracks.pop(track_sid, None)
        if state is not None:
            state.task.cancel()
        if self._primary_sid == track_sid:
            self._primary_sid = next(iter(self._tracks), None)

    async def _read_track_loop(
        self,
        track_sid: str,
        participant_identity: str,
        display_name: str,
        stream: rtc.AudioStream,
    ) -> None:
        try:
            async for event in stream:
                if self._closed:
                    break
                if not self._attached:
                    continue

                state = self._tracks.get(track_sid)
                if state is None:
                    break

                frame = event.frame
                now = time.monotonic()
                state.last_frame_ts = now
                rms = compute_rms_int16(frame)

                if rms >= SPEECH_RMS_THRESHOLD:
                    state.speaking_until = now + SPEECH_HANGOVER_SEC
                    if rms > 320.0 and (
                        rms >= self._active_speaker_rms * 0.85
                        or now - self._active_speaker_ts > 1.0
                    ):
                        self._active_speaker_rms = rms
                        self._active_speaker_ts = now
                        self.last_active_speaker = display_name

                is_speaking = now <= state.speaking_until

                # Si sólo hay 1 participante en la sala, enviar su frame directamente sin puerta
                # para que Silero VAD opere exactamente igual que en una sesión 1 a 1.
                if len(self._tracks) <= 1:
                    self._primary_sid = track_sid
                    self._last_primary_ts = now
                    self._out_ch.send_nowait(frame)
                    continue

                # Cuando hay 2+ participantes en la sala:
                # Si la pista acaba de empezar a hablar, volcar primero su pre-roll (100 ms)
                # para no cortar la primera consonante.
                if is_speaking:
                    if not state.was_speaking and state.preroll:
                        for pre_frame in state.preroll:
                            state.buffer.append(pre_frame)
                        state.preroll.clear()
                    state.buffer.append(frame)
                else:
                    state.preroll.append(frame)
                state.was_speaking = is_speaking

                # Failover automático de reloj si el micrófono primario está silenciado (>90 ms sin paquetes)
                if (
                    self._primary_sid is None
                    or self._primary_sid not in self._tracks
                    or (
                        self._primary_sid != track_sid
                        and now - self._last_primary_ts > 0.09
                    )
                ):
                    self._primary_sid = track_sid

                if self._primary_sid == track_sid:
                    self._last_primary_ts = now
                    self._emit_mixed_tick(frame)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Flujo de audio cerrado para %s", track_sid, exc_info=True)
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass
            self._tracks.pop(track_sid, None)
            if self._primary_sid == track_sid:
                self._primary_sid = next(iter(self._tracks), None)

    def _emit_mixed_tick(self, reference_frame: rtc.AudioFrame) -> None:
        """Emite exactamente un frame de 50 ms combinando a todos los participantes con voz activa."""
        active_frames: list[rtc.AudioFrame] = []
        for state in list(self._tracks.values()):
            if state.buffer:
                active_frames.append(state.buffer.popleft())

        if not active_frames:
            # Nadie está hablando en este pulso de 50 ms: emitir silencio limpio para que
            # Silero VAD detecte el fin de turno sin que el ruido ambiente de otro micrófono lo bloquee.
            self._out_ch.send_nowait(make_silence_frame(reference_frame))
            return

        self._out_ch.send_nowait(mix_int16_frames(active_frames))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._started:
            self._room.off("track_subscribed", self._on_track_subscribed)
            self._room.off("track_unsubscribed", self._on_track_unsubscribed)
            self._room.off("track_unpublished", self._on_track_unpublished)
            self._room.off("participant_disconnected", self._on_participant_disconnected)

        states = list(self._tracks.values())
        self._tracks.clear()
        self._primary_sid = None
        for st in states:
            st.task.cancel()
        if states:
            await asyncio.gather(*(st.task for st in states), return_exceptions=True)
        self._out_ch.close()
