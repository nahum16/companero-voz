import { randomUUID, timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { AccessToken } from "livekit-server-sdk";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function verifyAccessCode(provided: string, expected: string): boolean {
  const providedBuf = Buffer.from(provided, "utf8");
  const expectedBuf = Buffer.from(expected, "utf8");
  if (providedBuf.length !== expectedBuf.length) {
    return false;
  }
  return timingSafeEqual(providedBuf, expectedBuf);
}

function sanitizeRoomCode(raw?: unknown): string {
  if (typeof raw !== "string") return "";
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40);
}

function sanitizeUserName(raw?: unknown): string {
  if (typeof raw !== "string") return "Usuario";
  const cleaned = raw.trim().replace(/\s+/g, " ").slice(0, 32);
  return cleaned || "Usuario";
}

export async function POST(request: NextRequest) {
  // Leer variables en tiempo de ejecución para que `next build` no falle sin ellas
  const serverUrl = process.env.LIVEKIT_URL?.trim();
  const apiKey = process.env.LIVEKIT_API_KEY?.trim();
  const apiSecret = process.env.LIVEKIT_API_SECRET?.trim();

  if (!serverUrl || !apiKey || !apiSecret) {
    return NextResponse.json(
      { error: "Servidor sin configurar: faltan variables LIVEKIT_*" },
      { status: 500 },
    );
  }

  let body: {
    accessCode?: unknown;
    roomName?: unknown;
    userName?: unknown;
  } = {};
  try {
    body = (await request.json()) as typeof body;
  } catch {
    // Tolerar cuerpo vacío
  }

  const expectedCode = process.env.APP_ACCESS_CODE?.trim();
  if (expectedCode) {
    const accessCode =
      typeof body?.accessCode === "string" ? body.accessCode : "";
    if (!verifyAccessCode(accessCode, expectedCode)) {
      return NextResponse.json(
        { error: "Código de acceso incorrecto o ausente" },
        { status: 401 },
      );
    }
  }

  const userName = sanitizeUserName(body?.userName);
  const requestedCode = sanitizeRoomCode(body?.roomName);
  const sharedCode = requestedCode || randomUUID().slice(0, 8);
  // Si el usuario indica un código de sala compartida (o entra por un enlace ?sala=...),
  // todos los que usen ese código entran a la misma sala `sala-<codigo>`.
  const roomName = `sala-${sharedCode}`;
  const identity = `usuario-${randomUUID().slice(0, 8)}`;

  // No se configura despacho explícito: el agente Python no define `agent_name`,
  // por lo que LiveKit Cloud lo despacha automáticamente a toda sala nueva.
  const at = new AccessToken(apiKey, apiSecret, {
    identity,
    name: userName,
    ttl: "2h",
  });

  at.addGrant({
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canSubscribe: true,
    canPublishData: true,
  });

  const token = await at.toJwt();

  return NextResponse.json(
    {
      serverUrl,
      token,
      roomName,
      sharedCode,
      identity,
      userName,
    },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
