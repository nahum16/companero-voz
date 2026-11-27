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

  const expectedCode = process.env.APP_ACCESS_CODE?.trim();
  if (expectedCode) {
    let accessCode = "";
    try {
      const body = (await request.json()) as { accessCode?: unknown };
      if (typeof body?.accessCode === "string") {
        accessCode = body.accessCode;
      }
    } catch {
      // Tolerar cuerpo vacío o JSON inválido para responder 401 de forma uniforme
    }

    if (!verifyAccessCode(accessCode, expectedCode)) {
      return NextResponse.json(
        { error: "Código de acceso incorrecto o ausente" },
        { status: 401 },
      );
    }
  }

  const identity = `usuario-${randomUUID().slice(0, 8)}`;
  const roomName = `companero-${randomUUID()}`;

  // No se configura despacho explícito: el agente Python no define `agent_name`,
  // por lo que LiveKit Cloud lo despacha automáticamente a toda sala nueva.
  const at = new AccessToken(apiKey, apiSecret, {
    identity,
    name: "Usuario",
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
    { serverUrl, token, roomName, identity },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
