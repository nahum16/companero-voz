/**
 * Genera una URL Blob con un archivo WAV PCM de 16 bits mono casi silencioso
 * (alterna muestras 0 y ±2, ~-84 dBFS). No es silencio digital absoluto para que
 * Chrome Android / iOS lo consideren reproducción activa y mantengan la MediaSession.
 */
export function createNearSilentWavUrl(seconds = 10, sampleRate = 8000): string {
  const numSamples = seconds * sampleRate;
  const dataSize = numSamples * 2; // 16 bits = 2 bytes por muestra
  const buffer = new ArrayBuffer(44 + dataSize);
  const view = new DataView(buffer);

  const writeAscii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i += 1) {
      view.setUint8(offset + i, text.charCodeAt(i));
    }
  };

  // Cabecera RIFF/WAVE PCM mono de 44 bytes
  writeAscii(0, "RIFF");
  view.setUint32(4, 36 + dataSize, true);
  writeAscii(8, "WAVE");
  writeAscii(12, "fmt ");
  view.setUint32(16, 16, true); // tamaño del subchunk PCM
  view.setUint16(20, 1, true); // formato PCM = 1
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true); // byteRate
  view.setUint16(32, 2, true); // blockAlign
  view.setUint16(34, 16, true); // bitsPerSample
  writeAscii(36, "data");
  view.setUint32(40, dataSize, true);

  for (let i = 0; i < numSamples; i += 1) {
    const sample = i % 4 === 0 ? 2 : i % 4 === 2 ? -2 : 0;
    view.setInt16(44 + i * 2, sample, true);
  }

  const blob = new Blob([buffer], { type: "audio/wav" });
  return URL.createObjectURL(blob);
}

/**
 * Mantiene una pista <audio> casi silenciosa en bucle para que el sistema operativo
 * conserve vivo el proceso WebRTC y la MediaSession cuando la pantalla se apaga.
 */
export class KeepAlive {
  private audio: HTMLAudioElement | null = null;
  private url: string | null = null;

  start(): void {
    try {
      if (!this.audio) {
        this.url = createNearSilentWavUrl();
        const el = document.createElement("audio");
        el.src = this.url;
        el.loop = true;
        el.preload = "auto";
        el.setAttribute("playsinline", "");
        this.audio = el;
      }
      // Debe llamarse de forma síncrona dentro del gesto de usuario
      void this.audio.play().catch(() => {});
    } catch {
      // Ignorar restricciones de reproducción si no aplican
    }
  }

  ensurePlaying(): void {
    try {
      if (this.audio && this.audio.paused) {
        void this.audio.play().catch(() => {});
      }
    } catch {
      // No bloquear la llamada si falla la reanudación
    }
  }

  stop(): void {
    try {
      if (this.audio) {
        this.audio.pause();
        this.audio.removeAttribute("src");
        this.audio.load();
        this.audio = null;
      }
      if (this.url) {
        URL.revokeObjectURL(this.url);
        this.url = null;
      }
    } catch {
      // Ignorar errores al limpiar recursos
    }
  }
}

/**
 * Tonos breves de confirmación auditiva para usar con los audífonos sin mirar la pantalla.
 */
export class Earcons {
  private ctx: AudioContext | null = null;

  unlock(): void {
    try {
      const AudioCtx =
        window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext })
          .webkitAudioContext;
      if (!AudioCtx) return;
      if (!this.ctx) {
        this.ctx = new AudioCtx();
      }
      if (this.ctx.state === "suspended") {
        void this.ctx.resume().catch(() => {});
      }
    } catch {
      // AudioContext opcional
    }
  }

  private tone(freqs: number[], ms = 90): void {
    try {
      if (!this.ctx) return;
      if (this.ctx.state === "suspended") {
        void this.ctx.resume().catch(() => {});
      }

      const now = this.ctx.currentTime;
      const stepSec = ms / 1000;

      freqs.forEach((freq, idx) => {
        if (!this.ctx) return;
        const start = now + idx * (stepSec * 0.85);
        const end = start + stepSec;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();

        osc.type = "sine";
        osc.frequency.setValueAtTime(freq, start);

        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(0.08, start + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, end);

        osc.connect(gain);
        gain.connect(this.ctx.destination);
        osc.start(start);
        osc.stop(end + 0.01);
      });
    } catch {
      // Los earcons nunca deben romper el flujo de voz
    }
  }

  connected(): void {
    this.tone([660, 880], 90);
  }

  agentJoined(): void {
    this.tone([880], 100);
  }

  muted(): void {
    this.tone([520, 340], 85);
  }

  unmuted(): void {
    this.tone([340, 520], 85);
  }

  disconnected(): void {
    this.tone([440, 300], 110);
  }
}
