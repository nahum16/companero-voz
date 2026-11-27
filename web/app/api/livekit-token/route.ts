import { randomUUID, timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import {
  AccessToken,
  AgentDispatchClient,
  RoomServiceClient,
} from "livekit-server-sdk";

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

function toHttpUrl(wsUrl: string): string {
  if (wsUrl.startsWith("wss://")) return `https://${wsUrl.slice(6)}`;
  if (wsUrl.startsWith("ws://")) return `http://${wsUrl.slice(5)}`;
  return wsUrl;
}

async function ensureRoomReadyForAgent(
  serverUrl: string,
  apiKey: string,
  apiSecret: string,
  roomName: string,
): Promise<void> {
  try {
    const httpUrl = toHttpUrl(serverUrl);
    const roomService = new RoomServiceClient(httpUrl, apiKey, apiSecret);
    const participants = await roomService.listParticipants(roomName);
    const hasAgent = participants.some(
      (p) =>
        (p as unknown as { kind?: number }).kind === 4 ||
        p.identity.startsWith("agent-"),
    );

    if (!hasAgent) {
      if (participants.length === 0) {
        await roomService.deleteRoom(roomName);
      } else {
        const dispatchClient = new AgentDispatchClient(
          httpUrl,
          apiKey,
          apiSecret,
        );
        await dispatchClient.createDispatch(roomName, "");
      }
    }
  } catch {
    // Si la sala no existe aún (404), LiveKit la creará al entrar el usuario
    // y despachará al agente automáticamente.
  }
}

export async function POST(request: NextRequest) {
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
  const nameSlug =
    userName !== "Usuario" ? sanitizeRoomCode(userName).slice(0, 16) : "";
  const defaultCode = nameSlug
    ? `${nameSlug}-${randomUUID().slice(0, 4)}`
    : randomUUID().slice(0, 8);
  const sharedCode = requestedCode || defaultCode;
  const roomName = `sala-${sharedCode}`;
  const identity = `usuario-${randomUUID().slice(0, 8)}`;

  await ensureRoomReadyForAgent(serverUrl, apiKey, apiSecret, roomName);

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
