import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = resolve(__dirname, "../public/icons");

// Tabla CRC32 precalculada para firmar los chunks PNG
const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i += 1) {
    c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function makeChunk(type, data) {
  const typeBuf = Buffer.from(type, "ascii");
  const lenBuf = Buffer.alloc(4);
  lenBuf.writeUInt32BE(data.length, 0);

  const crcInput = Buffer.concat([typeBuf, data]);
  const crcBuf = Buffer.alloc(4);
  crcBuf.writeUInt32BE(crc32(crcInput), 0);

  return Buffer.concat([lenBuf, typeBuf, data, crcBuf]);
}

function mixColor(bg, fg, alpha) {
  const a = Math.max(0, Math.min(1, alpha));
  return [
    Math.round(bg[0] + (fg[0] - bg[0]) * a),
    Math.round(bg[1] + (fg[1] - bg[1]) * a),
    Math.round(bg[2] + (fg[2] - bg[2]) * a),
  ];
}

function renderIconPng(size, { maskable = false } = {}) {
  const bg = [11, 15, 20]; // #0b0f14
  const green = [34, 197, 94]; // #22c55e

  const center = (size - 1) / 2;
  // Para iconos maskable el contenido cabe dentro del 80% central (radio <= 0.36 * size)
  const outerR = size * (maskable ? 0.35 : 0.42);
  const ringMidR = outerR * 0.62;
  const ringHalfWidth = outerR * 0.08;
  const coreR = outerR * 0.28;

  const raw = Buffer.alloc(size * (1 + size * 4));

  for (let y = 0; y < size; y += 1) {
    const rowStart = y * (1 + size * 4);
    raw[rowStart] = 0; // filtro PNG None

    for (let x = 0; x < size; x += 1) {
      const dx = x - center;
      const dy = y - center;
      const dist = Math.hypot(dx, dy);

      // Antialias suave de 1 px sobre el borde exterior del círculo verde
      const outerAlpha = Math.max(0, Math.min(1, outerR + 0.5 - dist));
      let rgb = mixColor(bg, green, outerAlpha);

      // Anillo oscuro interior tipo onda sonora
      const ringDist = Math.abs(dist - ringMidR);
      const ringAlpha = Math.max(
        0,
        Math.min(1, ringHalfWidth + 0.5 - ringDist),
      );
      if (ringAlpha > 0) {
        rgb = mixColor(rgb, bg, ringAlpha);
      }

      // Núcleo oscuro central
      const coreAlpha = Math.max(0, Math.min(1, coreR + 0.5 - dist));
      if (coreAlpha > 0) {
        rgb = mixColor(rgb, bg, coreAlpha);
      }

      const px = rowStart + 1 + x * 4;
      raw[px] = rgb[0];
      raw[px + 1] = rgb[1];
      raw[px + 2] = rgb[2];
      raw[px + 3] = 255;
    }
  }

  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 6; // RGBA
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0;

  const idat = deflateSync(raw, { level: 9 });
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);

  return Buffer.concat([
    signature,
    makeChunk("IHDR", ihdr),
    makeChunk("IDAT", idat),
    makeChunk("IEND", Buffer.alloc(0)),
  ]);
}

mkdirSync(OUT_DIR, { recursive: true });

const targets = [
  { file: "icon-192.png", size: 192, maskable: false },
  { file: "icon-512.png", size: 512, maskable: false },
  { file: "icon-maskable-512.png", size: 512, maskable: true },
  { file: "apple-touch-icon.png", size: 180, maskable: false },
];

for (const { file, size, maskable } of targets) {
  const png = renderIconPng(size, { maskable });
  writeFileSync(resolve(OUT_DIR, file), png);
  console.log(`Generado ${file} (${size}x${size})`);
}
