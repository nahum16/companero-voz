import { NextResponse } from "next/server";
import { RoomServiceClient } from "livekit-server-sdk";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function toHttpUrl(wsUrl: string): string {
  if (wsUrl.startsWith("wss://")) return `https://${wsUrl.slice(6)}`;
  if (wsUrl.startsWith("ws://")) return `http://${wsUrl.slice(5)}`;
  return wsUrl;
}

function extractSharedCode(roomName: string): string {
  if (roomName.startsWith("sala-")) return roomName.slice(5);
  if (roomName.startsWith("companero-")) return roomName.slice(10);
  return roomName;
}

export async function GET() {
  const serverUrl = process.env.LIVEKIT_URL?.trim();
  const apiKey = process.env.LIVEKIT_API_KEY?.trim();
  const apiSecret = process.env.LIVEKIT_API_SECRET?.trim();

  if (!serverUrl || !apiKey || !apiSecret) {
    return NextResponse.json(
      { rooms: [], error: "Servidor sin configurar" },
      { status: 500 },
    );
  }

  try {
    const roomService = new RoomServiceClient(
      toHttpUrl(serverUrl),
      apiKey,
      apiSecret,
    );
    const liveRooms = await roomService.listRooms();

    const detailed = await Promise.all(
      liveRooms.map(async (room) => {
        try {
          const participants = await roomService.listParticipants(room.name);
          const humans: string[] = [];
          let hasAgent = false;

          for (const p of participants) {
            const kind = (p as unknown as { kind?: number }).kind;
            const isAgent = kind === 4 || p.identity.startsWith("agent-");
            if (isAgent) {
              hasAgent = true;
            } else {
              humans.push((p.name || "").trim() || p.identity);
            }
          }

          return {
            roomName: room.name,
            sharedCode: extractSharedCode(room.name),
            participants: humans,
            participantCount: humans.length,
            hasAgent,
          };
        } catch {
          return null;
        }
      }),
    );

    const activeRooms = detailed.filter(
      (r): r is NonNullable<typeof r> =>
        r !== null && r.participantCount > 0,
    );

    return NextResponse.json(
      { rooms: activeRooms },
      {
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  } catch {
    return NextResponse.json(
      { rooms: [] },
      {
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }
}
