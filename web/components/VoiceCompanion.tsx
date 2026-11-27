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

interface ActiveRoomInfo {
  roomName: string;
  sharedCode: string;
  participants: string[];
  participantCount: number;
  hasAgent: boolean;
}

interface QuotaInfo {
  model: string;
  totalKeys?: number;
  activeKeys?: number;
  limitRequests: number | null;
  remainingRequests: number | null;
  resetRequests: string | null;
  limitTokens: number | null;
  remainingTokens: number | null;
  resetTokens: string | null;
  updatedAt: string;
}

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

function getHumanParticipantsList(room: Room, localName: string): string[] {
  const names: string[] = [localName || "Tú"];
  for (const participant of room.remoteParticipants.values()) {
    if (participant.kind !== ParticipantKind.AGENT) {
      names.push(participant.name || participant.identity);
    }
  }
  return names;
}

function calcPercentage(remaining: number | null, limit: number | null): number {
  if (remaining === null || limit === null || limit <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((remaining / limit) * 100)));
}

export function VoiceCompanion() {
  const [status, setStatus] = useState<Status>("idle");
  const [muted, setMuted] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [needsAudioTap, setNeedsAudioTap] = useState(false);

  const [userName, setUserName] = useState("");
  const [roomInput, setRoomInput] = useState("");
  const [activeRoomCode, setActiveRoomCode] = useState<string | null>(null);
  const [participants, setParticipants] = useState<string[]>([]);
  const [copiedLink, setCopiedLink] = useState(false);
  const [activeRooms, setActiveRooms] = useState<ActiveRoomInfo[]>([]);

  const [quota, setQuota] = useState<QuotaInfo | null>(null);
  const [loadingQuota, setLoadingQuota] = useState(false);

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

  useEffect(() => {
    if (typeof window === "undefined") return;
    const savedName = window.localStorage.getItem("companionUserName") ?? "";
    if (savedName) {
      setUserName(savedName);
    }
    const params = new URLSearchParams(window.location.search);
    const salaParam = params.get("sala")?.trim() ?? "";
    if (salaParam) {
      setRoomInput(salaParam);
    }
  }, []);

  const refreshQuota = useCallback(async (force = false) => {
    setLoadingQuota(true);
    try {
      const url = force ? "/api/groq-quota?refresh=1" : "/api/groq-quota";
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as QuotaInfo;
      if (data && typeof data.model === "string") {
        setQuota(data);
      }
    } catch {
      // Ignorar errores de red al consultar cuota
    } finally {
      setLoadingQuota(false);
    }
  }, []);

  useEffect(() => {
    void refreshQuota(false);
  }, [refreshQuota]);

  const refreshActiveRooms = useCallback(async () => {
    try {
      const res = await fetch("/api/livekit-rooms", { cache: "no-store" });
      if (!res.ok) return;
      const data = (await res.json()) as { rooms?: ActiveRoomInfo[] };
      if (Array.isArray(data.rooms)) {
        setActiveRooms(data.rooms);
      }
    } catch {
      // Silenciar errores de red durante el polling de salas
    }
  }, []);

  useEffect(() => {
    if (status !== "idle" && status !== "error") return;
    void refreshActiveRooms();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void refreshActiveRooms();
      }
    }, 3500);
    return () => window.clearInterval(timer);
  }, [refreshActiveRooms, status]);

  const cleanup = useCallback(
    (playTone = true) => {
      keepAliveRef.current?.stop();
      clearMediaSession();

      if (audioContainerRef.current) {
        audioContainerRef.current.replaceChildren();
      }

      roomRef.current = null;
      mutedRef.current = false;
      setMuted(false);
      setNeedsAudioTap(false);
      setActiveRoomCode(null);
      setParticipants([]);
      setCopiedLink(false);
      setStatus("idle");

      if (playTone) {
        earconsRef.current?.disconnected();
      }
      void refreshActiveRooms();
      void refreshQuota(true);
    },
    [refreshActiveRooms, refreshQuota],
  );

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

  const fetchToken = useCallback(
    async (trimmedName: string, trimmedRoom: string) => {
      const storedCode =
        window.localStorage.getItem("accessCode") ?? undefined;
      const requestToken = (accessCode?: string) =>
        fetch("/api/livekit-token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ...(accessCode ? { accessCode } : {}),
            ...(trimmedName ? { userName: trimmedName } : {}),
            ...(trimmedRoom ? { roomName: trimmedRoom } : {}),
          }),
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
        sharedCode?: string;
        userName?: string;
        error?: string;
      };

      if (!res.ok || !data.serverUrl || !data.token) {
        throw new Error(data.error ?? "No se pudo obtener el token de sala");
      }

      return {
        serverUrl: data.serverUrl,
        token: data.token,
        sharedCode: data.sharedCode ?? trimmedRoom,
        resolvedName: data.userName ?? (trimmedName || "Tú"),
      };
    },
    [],
  );

  const connect = useCallback(
    async (overrideRoomCode?: string) => {
      if (roomRef.current || status === "connecting") return;

      // ORDEN CRÍTICO: iniciar keepAlive y desbloquear AudioContext de forma síncrona
      // dentro del gesto del usuario antes de cualquier `await`.
      keepAliveRef.current?.start();
      earconsRef.current?.unlock();

      const trimmedName = userName.trim();
      const trimmedRoom = (overrideRoomCode ?? roomInput).trim();
      if (overrideRoomCode !== undefined) {
        setRoomInput(overrideRoomCode);
      }
      if (trimmedName) {
        window.localStorage.setItem("companionUserName", trimmedName);
      }

      setErrorMsg(null);
      setNeedsAudioTap(false);
      setStatus("connecting");

      let room: Room | null = null;
      try {
        const { serverUrl, token, sharedCode, resolvedName } = await fetchToken(
          trimmedName,
          trimmedRoom,
        );

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

        const syncParticipants = () => {
          if (!room) return;
          setParticipants(getHumanParticipantsList(room, resolvedName));
        };

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
          } else {
            syncParticipants();
          }
        });

        room.on(RoomEvent.ParticipantDisconnected, (participant) => {
          if (participant.kind === ParticipantKind.AGENT) {
            setStatus("waiting");
          } else {
            syncParticipants();
          }
        });

        room.on(
          RoomEvent.ParticipantAttributesChanged,
          (_changed, participant) => {
            if (participant instanceof RemoteParticipant) {
              mapAgentState(participant);
            }
          },
        );

        room.on(RoomEvent.Reconnecting, () => {
          setStatus("reconnecting");
        });

        room.on(RoomEvent.Reconnected, () => {
          if (room) {
            syncParticipants();
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

        setActiveRoomCode(sharedCode || null);
        syncParticipants();

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
    },
    [cleanup, fetchToken, hangup, roomInput, status, toggleMute, userName],
  );

  const handleShareRoom = useCallback(async () => {
    if (!activeRoomCode || typeof window === "undefined") return;
    const shareUrl = `${window.location.origin}/?sala=${encodeURIComponent(activeRoomCode)}`;

    try {
      if (navigator.share) {
        await navigator.share({
          title: "Compañero de Voz — Sala compartida",
          text: `Únete a nuestra conversación con el compañero de voz (sala: ${activeRoomCode})`,
          url: shareUrl,
        });
        return;
      }
    } catch {
      // Si el usuario cancela el sheet nativo o falla, copiamos al portapapeles
    }

    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopiedLink(true);
      window.setTimeout(() => setCopiedLink(false), 2500);
    } catch {
      window.prompt("Copia este enlace para invitar a más personas:", shareUrl);
    }
  }, [activeRoomCode]);

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

  const reqPct = calcPercentage(
    quota?.remainingRequests ?? null,
    quota?.limitRequests ?? null,
  );
  const tokPct = calcPercentage(
    quota?.remainingTokens ?? null,
    quota?.limitTokens ?? null,
  );

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

        {!isConnected && !isConnecting && (
          <>
            <div className="room-form">
              <input
                type="text"
                className="room-input"
                placeholder="Tu nombre (opcional, ej. Carlos)"
                value={userName}
                maxLength={32}
                onChange={(e) => setUserName(e.target.value)}
                aria-label="Tu nombre"
              />
              <input
                type="text"
                className="room-input"
                placeholder="Nombre de sala (vacío = nueva sala)"
                value={roomInput}
                maxLength={40}
                onChange={(e) => setRoomInput(e.target.value)}
                aria-label="Código de sala compartida"
              />
            </div>

            <div className="active-rooms-box">
              <div className="active-rooms-header">
                <span className="live-indicator" aria-hidden="true" />
                <span>Salas activas en vivo ({activeRooms.length})</span>
              </div>

              {activeRooms.length === 0 ? (
                <p className="active-rooms-empty">
                  Nadie conectado ahora. Pulsa Conectar para abrir una sala.
                </p>
              ) : (
                <ul className="active-rooms-list">
                  {activeRooms.map((r) => (
                    <li key={r.roomName} className="active-room-item">
                      <div className="active-room-info">
                        <span className="active-room-title">
                          Sala <strong>{r.sharedCode}</strong>
                        </span>
                        <span className="active-room-people">
                          {r.participants.join(", ")} (
                          {r.participantCount}{" "}
                          {r.participantCount === 1 ? "persona" : "personas"})
                        </span>
                      </div>
                      <button
                        type="button"
                        className="join-room-btn"
                        onClick={() => void connect(r.sharedCode)}
                      >
                        Unirse
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </>
        )}

        {isConnected && (
          <div className="connected-controls">
            <div className="room-badge">
              <span>
                Sala <strong>{activeRoomCode}</strong> ·{" "}
                {participants.length}{" "}
                {participants.length === 1 ? "persona" : "personas"} (
                {participants.join(", ")})
              </span>
            </div>

            <div className="action-row">
              <button
                type="button"
                className="mute-button"
                onClick={() => void toggleMute()}
              >
                {muted ? "Activar micrófono" : "Silenciar micrófono"}
              </button>

              {activeRoomCode && (
                <button
                  type="button"
                  className="share-button"
                  onClick={() => void handleShareRoom()}
                >
                  {copiedLink ? "¡Enlace copiado!" : "Invitar a la sala"}
                </button>
              )}
            </div>
          </div>
        )}

        {quota && (
          <div className="quota-box" aria-label="Cuota gratuita de IA">
            <div className="quota-header">
              <span>
                Cuota Groq ({quota.activeKeys ?? 1}/{quota.totalKeys ?? 1}{" "}
                {(quota.totalKeys ?? 1) === 1 ? "cuenta" : "cuentas"})
              </span>
              <button
                type="button"
                className="quota-refresh-btn"
                onClick={() => void refreshQuota(true)}
                disabled={loadingQuota}
              >
                {loadingQuota ? "…" : "↻ Actualizar"}
              </button>
            </div>

            <div className="quota-item">
              <div className="quota-row">
                <span>Respuestas hoy (RPD)</span>
                <strong>
                  {quota.remainingRequests?.toLocaleString("es") ?? "—"} /{" "}
                  {quota.limitRequests?.toLocaleString("es") ?? "—"} ({reqPct}%)
                </strong>
              </div>
              <div className="quota-bar">
                <div
                  className="quota-bar-fill"
                  style={{ width: `${reqPct}%` }}
                />
              </div>
            </div>

            <div className="quota-item">
              <div className="quota-row">
                <span>Tokens por minuto (TPM)</span>
                <strong>
                  {quota.remainingTokens?.toLocaleString("es") ?? "—"} /{" "}
                  {quota.limitTokens?.toLocaleString("es") ?? "—"} ({tokPct}%)
                </strong>
              </div>
              <div className="quota-bar">
                <div
                  className="quota-bar-fill is-tokens"
                  style={{ width: `${tokPct}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {errorMsg && (
          <p className="error-text" role="alert">
            {errorMsg}
          </p>
        )}
      </section>

      <footer className="hint-footer">
        Puedes apagar la pantalla. Play/pausa de tus audífonos = silenciar/activar
        micrófono. Cualquier persona puede unirse a tu sala activa desde la lista.
      </footer>
    </main>
  );
}
