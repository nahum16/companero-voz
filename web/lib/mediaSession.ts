export interface MediaSessionCallbacks {
  onToggleMute: () => void;
  onHangup: () => void;
}

const ARTWORK = [
  { src: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
  { src: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
];

const MANAGED_ACTIONS = [
  "play",
  "pause",
  "togglemicrophone",
  "stop",
  "hangup",
] as const;

function safeSetHandler(
  action: string,
  handler: MediaSessionActionHandler | null,
): void {
  try {
    navigator.mediaSession.setActionHandler(
      action as MediaSessionAction,
      handler,
    );
  } catch {
    // No todos los navegadores soportan todas las acciones (ej. togglemicrophone/hangup)
  }
}

export function setupMediaSession({
  onToggleMute,
  onHangup,
}: MediaSessionCallbacks): void {
  if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
    return;
  }

  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: "Compañero de voz",
      artist: "Te escucho",
      album: "En vivo",
      artwork: ARTWORK,
    });
    navigator.mediaSession.playbackState = "playing";
  } catch {
    // Ignorar si el navegador restringe MediaMetadata
  }

  // En audífonos Bluetooth, el botón físico suele alternar entre play y pause;
  // ambos alternan el silencio del micrófono sin cortar la llamada.
  safeSetHandler("play", () => onToggleMute());
  safeSetHandler("pause", () => onToggleMute());
  safeSetHandler("togglemicrophone", () => onToggleMute());
  safeSetHandler("stop", () => onHangup());
  safeSetHandler("hangup", () => onHangup());
}

export function updateMediaSession(muted: boolean): void {
  if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
    return;
  }

  try {
    navigator.mediaSession.metadata = new MediaMetadata({
      title: "Compañero de voz",
      artist: muted ? "Micrófono silenciado" : "Te escucho",
      album: "En vivo",
      artwork: ARTWORK,
    });
    // Mantener siempre 'playing' para no ceder el foco multimedia en segundo plano
    navigator.mediaSession.playbackState = "playing";
  } catch {
    // Ignorar errores de actualización
  }

  try {
    const ms = navigator.mediaSession as unknown as {
      setMicrophoneActive?: (active: boolean) => void;
    };
    ms.setMicrophoneActive?.(!muted);
  } catch {
    // API experimental disponible solo en algunos navegadores Chromium
  }
}

export function clearMediaSession(): void {
  if (typeof navigator === "undefined" || !("mediaSession" in navigator)) {
    return;
  }

  for (const action of MANAGED_ACTIONS) {
    safeSetHandler(action, null);
  }

  try {
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  } catch {
    // Ignorar errores al limpiar la sesión multimedia
  }
}
