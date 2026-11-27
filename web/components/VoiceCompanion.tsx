"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  AudioPresets,
  ParticipantKind,
  RemoteParticipant,
  Room,
  RoomEvent,
  Track,
} from "livekit-client";
import { Earcons, KeepAlive } from "@/lib/audio";
import {
  clearMediaSession,
  setupMediaSession,
  updateMediaSession,
} from "@/lib/mediaSession";

type Status =
  | "idle"
  | "connecting"
  | "waiting"
  | "listening"
  | "thinking"
  | "speaking"
  | "reconnecting"
  | "error";

const STATUS_LABELS: Record<Status, string> = {
  idle: "Listo para conectar",
  connecting: "Conectando…",
  waiting: "Esperando al compañero…",
  listening: "Te escucho",
  thinking: "Pensando…",
  speaking: "Hablando…",
  reconnecting: "Reconectando…",
  error: "Error de conexión",
};

const AUDIO_CAPTURE_OPTIONS = {
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
  channelCount: 1,
} as const;

function hasAgentParticipant(room: Room): boolean {
  for (const participant of room.remoteParticipants.values()) {
    if (participant.kind === ParticipantKind.AGENT) {
      return true;
    }
  }
  return false;
}

export function VoiceCompanion() {
  const [status, setStatus] = useState<Status>("idle");
  const [muted, setMuted] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [needsAudioTap, setNeedsAudioTap] = useState(false);

  const roomRef = useRef<Room | null>(null);
  const mutedRef = useRef(false);
  const keepAliveRef = useRef<KeepAlive | null>(null);
  const earconsRef = useRef<Earcons | null>(null);
  const audioContainerRef = useRef<HTMLDivElement | null>(null);

  if (!keepAliveRef.current && typeof window !== "undefined") {
    keepAliveRef.current = new KeepAlive();
  }
  if (!earconsRef.current && typeof window !== "undefined") {
    earconsRef.current = new Earcons();
  }

  const cleanup = useCallback((playTone = true) => {
    keepAliveRef.current?.stop();
    clearMediaSession();

    if (audioContainerRef.current) {
      audioContainerRef.current.replaceChildren();
    }

    roomRef.current = null;
    mutedRef.current = false;
    setMuted(false);
    setNeedsAudioTap(false);
    setStatus("idle");

    if (playTone) {
      earconsRef.current?.disconnected();
    }
  }, []);

  const toggleMute = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;

    const next = !mutedRef.current;
    try {
      if (next) {
        await room.localParticipant.setMicrophoneEnabled(false);
      } else {
        await room.localParticipant.setMicrophoneEnabled(
          true,
          AUDIO_CAPTURE_OPTIONS,
        );
      }
      mutedRef.current = next;
      setMuted(next);

      if (next) {
        earconsRef.current?.muted();
      } else {
        earconsRef.current?.unmuted();
      }

      if (typeof navigator !== "undefined" && "vibrate" in navigator) {
        navigator.vibrate(next ? [60, 40, 60] : 80);
      }

      updateMediaSession(next);
      // Algunos navegadores pausan la pista auxiliar al pulsar play/pausa en los audífonos
      keepAliveRef.current?.ensurePlaying();
    } catch {
      setErrorMsg("No se pudo cambiar el estado del micrófono");
    }
  }, []);

  const hangup = useCallback(async () => {
    const room = roomRef.current;
    if (!room) {
      cleanup(false);
      return;
    }
    try {
      await room.disconnect();
    } finally {
      if (roomRef.current === room) {
        cleanup(true);
      }
    }
  }, [cleanup]);

  const fetchToken = useCallback(async () => {
    const storedCode = window.localStorage.getItem("accessCode") ?? undefined;
    const requestToken = (accessCode?: string) =>
      fetch("/api/livekit-token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(accessCode ? { accessCode } : {}),
      });

    let res = await requestToken(storedCode);

    if (res.status === 401) {
      const prompted = window.prompt("Código de acceso");
      if (!prompted) {
        throw new Error("Código de acceso requerido");
      }
      window.localStorage.setItem("accessCode", prompted);
      res = await requestToken(prompted);
    }

    const data = (await res.json()) as {
      serverUrl?: string;
      token?: string;
      error?: string;
    };

    if (!res.ok || !data.serverUrl || !data.token) {
      throw new Error(data.error ?? "No se pudo obtener el token de sala");
    }

    return { serverUrl: data.serverUrl, token: data.token };
  }, []);

  const connect = useCallback(async () => {
    if (roomRef.current || status === "connecting") return;

    // ORDEN CRÍTICO: iniciar keepAlive y desbloquear AudioContext de forma síncrona
    // dentro del gesto del usuario antes de cualquier `await`.
    keepAliveRef.current?.start();
    earconsRef.current?.unlock();

    setErrorMsg(null);
    setNeedsAudioTap(false);
    setStatus("connecting");

    let room: Room | null = null;
    try {
      const { serverUrl, token } = await fetchToken();

      room = new Room({
        adaptiveStream: false,
        dynacast: false,
        audioCaptureDefaults: AUDIO_CAPTURE_OPTIONS,
        publishDefaults: {
          audioPreset: AudioPresets.speech,
          dtx: true,
          red: true,
          stopMicTrackOnMute: false,
        },
      });
      roomRef.current = room;

      const mapAgentState = (participant: RemoteParticipant) => {
        if (participant.kind !== ParticipantKind.AGENT) return;
        const agentState = participant.attributes["lk.agent.state"];
        if (agentState === "speaking") {
          setStatus("speaking");
        } else if (agentState === "thinking") {
          setStatus("thinking");
        } else {
          setStatus("listening");
        }
      };

      room.on(RoomEvent.TrackSubscribed, (track) => {
        if (track.kind !== Track.Kind.Audio) return;
        const el = track.attach();
        el.setAttribute("playsinline", "");
        audioContainerRef.current?.appendChild(el);
      });

      room.on(RoomEvent.TrackUnsubscribed, (track) => {
        track.detach().forEach((el) => el.remove());
      });

      room.on(RoomEvent.ParticipantConnected, (participant) => {
        if (participant.kind === ParticipantKind.AGENT) {
          setStatus("listening");
          earconsRef.current?.agentJoined();
        }
      });

      room.on(RoomEvent.ParticipantDisconnected, (participant) => {
        if (participant.kind === ParticipantKind.AGENT) {
          setStatus("waiting");
        }
      });

      room.on(RoomEvent.ParticipantAttributesChanged, (_changed, participant) => {
        if (participant instanceof RemoteParticipant) {
          mapAgentState(participant);
        }
      });

      room.on(RoomEvent.Reconnecting, () => {
        setStatus("reconnecting");
      });

      room.on(RoomEvent.Reconnected, () => {
        if (room) {
          setStatus(hasAgentParticipant(room) ? "listening" : "waiting");
        }
      });

      room.on(RoomEvent.Disconnected, () => {
        if (roomRef.current === room) {
          cleanup(true);
        }
      });

      room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
        if (room && !room.canPlaybackAudio) {
          setNeedsAudioTap(true);
        } else {
          setNeedsAudioTap(false);
        }
      });

      room.on(RoomEvent.MediaDevicesError, () => {
        setErrorMsg("No se pudo acceder al micrófono");
        setStatus("error");
      });

      await room.connect(serverUrl, token);
      await room.localParticipant.setMicrophoneEnabled(
        true,
        AUDIO_CAPTURE_OPTIONS,
      );
      await room.startAudio();
      setNeedsAudioTap(!room.canPlaybackAudio);

      setupMediaSession({
        onToggleMute: () => {
          void toggleMute();
        },
        onHangup: () => {
          void hangup();
        },
      });
      earconsRef.current?.connected();

      setStatus(hasAgentParticipant(room) ? "listening" : "waiting");
    } catch (err) {
      if (room) {
        await room.disconnect().catch(() => {});
      }
      cleanup(false);
      const message =
        err instanceof Error ? err.message : "Error inesperado al conectar";
      setErrorMsg(message);
      setStatus("error");
    }
  }, [cleanup, fetchToken, hangup, status, toggleMute]);

  useEffect(() => {
    const handleVisibilityChange = () => {
      const room = roomRef.current;
      if (document.visibilityState !== "visible" || !room) return;

      void room.startAudio().catch(() => {});
      keepAliveRef.current?.ensurePlaying();

      const micPub = room.localParticipant.getTrackPublication(
        Track.Source.Microphone,
      );
      const mediaTrack = micPub?.track?.mediaStreamTrack;
      if (mediaTrack && mediaTrack.readyState === "ended" && !mutedRef.current) {
        void (async () => {
          try {
            await room.localParticipant.setMicrophoneEnabled(false);
            await room.localParticipant.setMicrophoneEnabled(
              true,
              AUDIO_CAPTURE_OPTIONS,
            );
          } catch {
            // Ignorar si el navegador aún está recuperando permisos
          }
        })();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => {
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      const activeRoom = roomRef.current;
      if (activeRoom) {
        roomRef.current = null;
        keepAliveRef.current?.stop();
        clearMediaSession();
        void activeRoom.disconnect().catch(() => {});
      }
    };
  }, []);

  const isConnected =
    status !== "idle" && status !== "error" && status !== "connecting";
  const isConnecting = status === "connecting";
  const isPulsing = status === "listening" || status === "speaking";

  const handleMainButtonClick = () => {
    if (needsAudioTap && roomRef.current) {
      void roomRef.current.startAudio().then(() => {
        if (roomRef.current?.canPlaybackAudio) {
          setNeedsAudioTap(false);
        }
      });
      keepAliveRef.current?.ensurePlaying();
      return;
    }

    if (isConnected) {
      void hangup();
    } else {
      void connect();
    }
  };

  const statusText = needsAudioTap
    ? "Toca para activar el audio"
    : muted && isConnected
      ? "Micrófono en silencio"
      : STATUS_LABELS[status];

  const mainButtonLabel = needsAudioTap
    ? "Activar audio"
    : isConnected
      ? "Colgar"
      : isConnecting
        ? "Conectando…"
        : "Conectar";

  return (
    <main className="companion-shell">
      <div ref={audioContainerRef} hidden aria-hidden="true" />

      <section className="companion-center" aria-live="polite">
        <div
          className={`button-ring state-${status}${isPulsing ? " is-pulsing" : ""}`}
        >
          <button
            type="button"
            className={`main-button ${isConnected ? "is-connected" : "is-idle"}`}
            onClick={handleMainButtonClick}
            disabled={isConnecting}
          >
            {mainButtonLabel}
          </button>
        </div>

        <div className="status-pill">
          <span className={`status-dot state-${status}`} aria-hidden="true" />
          <span>{statusText}</span>
        </div>

        {isConnected && (
          <button
            type="button"
            className="mute-button"
            onClick={() => void toggleMute()}
          >
            {muted ? "Activar micrófono" : "Silenciar micrófono"}
          </button>
        )}

        {errorMsg && (
          <p className="error-text" role="alert">
            {errorMsg}
          </p>
        )}
      </section>

      <footer className="hint-footer">
        Puedes apagar la pantalla. Play/pausa de tus audífonos = silenciar/activar
        micrófono.
      </footer>
    </main>
  );
}
