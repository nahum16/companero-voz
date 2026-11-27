import asyncio
import collections
import logging
import time
from collections.abc import AsyncIterator

import numpy as np
from livekit import rtc
from livekit.agents.voice import io as voice_io

logger = logging.getLogger("companero.multi_audio")


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


def compute_rms_int16(frame: rtc.AudioFrame) -> float:
    """Calcula la energía RMS de un frame PCM de 16 bits."""
    samples = np.frombuffer(frame.data, dtype=np.int16)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)))


class MultiParticipantAudioInput(voice_io.AudioInput):
    """
    Entrada de audio que escucha y mezcla simultáneamente los micrófonos de todos
    los participantes humanos conectados a la sala, sin bloquearse cuando alguien
    silencia su micrófono y rastreando quién es el hablante activo por energía RMS.
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

        self._track_queues: dict[str, collections.deque[rtc.AudioFrame]] = {}
        self._track_last_active: dict[str, float] = {}
        self._track_tasks: dict[str, asyncio.Task[None]] = {}
        self._frame_event = asyncio.Event()
        self._last_emit_ts = 0.0

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
        for pub in participant.track_publications.values():
            self._stop_track_stream(pub.sid)

    def _start_track_stream(
        self,
        track: rtc.RemoteTrack,
        participant: rtc.RemoteParticipant,
    ) -> None:
        track_sid = track.sid
        if track_sid in self._track_tasks:
            return

        display_name = (participant.name or "").strip() or participant.identity
        logger.info(
            "Escuchando micrófono de %s (%s)",
            display_name,
            participant.identity,
        )
        stream = rtc.AudioStream.from_track(
            track=track,
            sample_rate=self._sample_rate,
            num_channels=self._num_channels,
            frame_size_ms=self._frame_size_ms,
        )
        self._track_queues[track_sid] = collections.deque(maxlen=6)
        task = asyncio.create_task(
            self._read_track_loop(track_sid, participant, stream),
        )
        self._track_tasks[track_sid] = task

    def _stop_track_stream(self, track_sid: str) -> None:
        task = self._track_tasks.pop(track_sid, None)
        if task is not None:
            task.cancel()
        self._track_queues.pop(track_sid, None)
        self._track_last_active.pop(track_sid, None)

    async def _read_track_loop(
        self,
        track_sid: str,
        participant: rtc.RemoteParticipant,
        stream: rtc.AudioStream,
    ) -> None:
        try:
            async for event in stream:
                if self._closed:
                    break
                if not self._attached:
                    continue

                frame = event.frame
                rms = compute_rms_int16(frame)
                now = time.monotonic()
                if rms > 350.0 and (
                    rms >= self._active_speaker_rms * 0.85
                    or now - self._active_speaker_ts > 1.2
                ):
                    self._active_speaker_rms = rms
                    self._active_speaker_ts = now
                    self.last_active_speaker = (
                        (participant.name or "").strip() or participant.identity
                    )

                queue = self._track_queues.get(track_sid)
                if queue is not None:
                    queue.append(frame)
                    self._track_last_active[track_sid] = now
                    self._frame_event.set()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Flujo de audio cerrado para %s", track_sid, exc_info=True)
        finally:
            try:
                await stream.aclose()
            except Exception:
                pass
            self._track_queues.pop(track_sid, None)
            self._track_last_active.pop(track_sid, None)
            self._track_tasks.pop(track_sid, None)

    async def __anext__(self) -> rtc.AudioFrame:
        while not self._closed:
            now = time.monotonic()
            active_streams = sum(
                1 for ts in self._track_last_active.values() if now - ts < 0.15
            )
            # Cuando 2 o más micrófonos están abiertos enviando paquetes al mismo tiempo,
            # acompasar la ventana a ~46 ms para mezclar un frame por participante
            # y mantener la velocidad exacta 1x de tiempo real.
            if active_streams > 1 and self._last_emit_ts > 0:
                target_interval = (self._frame_size_ms / 1000.0) * 0.92
                remaining = target_interval - (now - self._last_emit_ts)
                if remaining > 0.002:
                    await asyncio.sleep(remaining)

            frames: list[rtc.AudioFrame] = []
            for queue in list(self._track_queues.values()):
                if queue:
                    frames.append(queue.popleft())

            if not frames:
                self._frame_event.clear()
                if any(self._track_queues.values()):
                    self._frame_event.set()
                    continue
                await self._frame_event.wait()
                continue

            self._last_emit_ts = time.monotonic()
            return mix_int16_frames(frames)

        raise StopAsyncIteration

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._frame_event.set()

        if self._started:
            self._room.off("track_subscribed", self._on_track_subscribed)
            self._room.off("track_unsubscribed", self._on_track_unsubscribed)
            self._room.off("track_unpublished", self._on_track_unpublished)
            self._room.off("participant_disconnected", self._on_participant_disconnected)

        tasks = list(self._track_tasks.values())
        self._track_tasks.clear()
        self._track_queues.clear()
        self._track_last_active.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
