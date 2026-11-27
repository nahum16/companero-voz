import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import numpy as np
from livekit import rtc
from livekit.agents import utils
from livekit.agents.voice import io as voice_io

logger = logging.getLogger("companero.multi_audio")

# Umbral de energía de voz para cambiar el foco al nuevo hablante o mezclar voces simultáneas
FOCUS_SWITCH_RMS = 160.0
OVERLAP_SPEECH_RMS = 260.0


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
    latest_frame: rtc.AudioFrame | None = None
    last_frame_ts: float = 0.0
    last_rms: float = 0.0
    last_voice_ts: float = 0.0


class MultiParticipantAudioInput(voice_io.AudioInput):
    """
    Selector de hablante activo y mezclador multipersona sin retardo de cola.

    - Mantiene el foco en el participante que está hablando (o que habló por última vez)
      y reenvía sus frames originales de WebRTC sin recortar consonantes suaves ni alterar
      el piso natural de audio que espera Silero VAD.
    - En cuanto otra persona en la sala empieza a hablar con mayor energía RMS, conmuta
      instantáneamente el foco hacia su micrófono (o mezcla ambos si hablan a la vez).
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
        self._focused_sid: str | None = None

        self.last_active_speaker: str | None = None

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
        if self._focused_sid is None:
            self._focused_sid = track_sid
            self.last_active_speaker = display_name
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
        if self._focused_sid == track_sid:
            self._focused_sid = next(iter(self._tracks), None)

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
                rms = compute_rms_int16(frame)
                state.latest_frame = frame
                state.last_frame_ts = now
                state.last_rms = rms

                if rms >= FOCUS_SWITCH_RMS:
                    state.last_voice_ts = now

                focused = self._tracks.get(self._focused_sid or "")

                # Cambiar el foco a esta pista si:
                # 1. No hay pista enfocada o la pista enfocada lleva >90 ms sin enviar frames (muteada)
                # 2. Esta pista tiene voz activa y supera claramente la energía actual de la pista enfocada
                if (
                    focused is None
                    or (focused.track_sid != track_sid and now - focused.last_frame_ts > 0.09)
                    or (
                        focused.track_sid != track_sid
                        and rms >= FOCUS_SWITCH_RMS
                        and rms > focused.last_rms * 1.25
                        and (now - focused.last_voice_ts > 0.35 or rms > focused.last_rms * 1.8)
                    )
                ):
                    self._focused_sid = track_sid
                    focused = state

                if rms >= FOCUS_SWITCH_RMS and self._focused_sid == track_sid:
                    self.last_active_speaker = display_name

                # Sólo la pista que tiene el foco marca el reloj de salida de 50 ms (20 frames/s)
                if self._focused_sid == track_sid:
                    to_mix = [frame]
                    for other_sid, other in list(self._tracks.items()):
                        if (
                            other_sid != track_sid
                            and other.latest_frame is not None
                            and now - other.last_frame_ts < 0.09
                            and other.last_rms >= OVERLAP_SPEECH_RMS
                        ):
                            to_mix.append(other.latest_frame)
                            other.latest_frame = None
                    self._out_ch.send_nowait(mix_int16_frames(to_mix))
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
            if self._focused_sid == track_sid:
                self._focused_sid = next(iter(self._tracks), None)

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
        self._focused_sid = None
        for st in states:
            st.task.cancel()
        if states:
            await asyncio.gather(*(st.task for st in states), return_exceptions=True)
        self._out_ch.close()
