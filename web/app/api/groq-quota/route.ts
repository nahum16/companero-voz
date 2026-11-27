import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface QuotaSnapshot {
  model: string;
  totalKeys: number;
  activeKeys: number;
  limitRequests: number | null;
  remainingRequests: number | null;
  resetRequests: string | null;
  limitTokens: number | null;
  remainingTokens: number | null;
  resetTokens: string | null;
  updatedAt: string;
}

let cachedSnapshot: { data: QuotaSnapshot; expiresAt: number } | null = null;
const CACHE_TTL_MS = 60_000;

function parseHeaderInt(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function getGroqKeys(): string[] {
  const multi = process.env.GROQ_API_KEYS?.trim() ?? "";
  const single = process.env.GROQ_API_KEY?.trim() ?? "";
  const raw = [
    ...multi.split(",").map((k) => k.trim()),
    single,
  ].filter(Boolean);
  return Array.from(new Set(raw));
}

export async function GET(request: NextRequest) {
  const keys = getGroqKeys();
  const model = process.env.GROQ_LLM_MODEL?.trim() || "qwen/qwen3.8-27b";

  if (keys.length === 0) {
    return NextResponse.json(
      { error: "Falta configurar GROQ_API_KEY / GROQ_API_KEYS en el servidor web" },
      { status: 500 },
    );
  }

  const forceRefresh = request.nextUrl.searchParams.get("refresh") === "1";
  const now = Date.now();

  if (!forceRefresh && cachedSnapshot && now < cachedSnapshot.expiresAt) {
    return NextResponse.json(cachedSnapshot.data, {
      headers: { "Cache-Control": "no-store" },
    });
  }

  try {
    const probes = await Promise.all(
      keys.map(async (key) => {
        try {
          const res = await fetch(
            "https://api.groq.com/openai/v1/chat/completions",
            {
              method: "POST",
              headers: {
                Authorization: `Bearer ${key}`,
                "Content-Type": "application/json",
              },
              body: JSON.stringify({
                model,
                messages: [{ role: "user", content: "." }],
                max_completion_tokens: 1,
              }),
              cache: "no-store",
            },
          );

          return {
            ok: res.ok,
            limitRequests: parseHeaderInt(
              res.headers.get("x-ratelimit-limit-requests"),
            ),
            remainingRequests: parseHeaderInt(
              res.headers.get("x-ratelimit-remaining-requests"),
            ),
            resetRequests: res.headers.get("x-ratelimit-reset-requests"),
            limitTokens: parseHeaderInt(
              res.headers.get("x-ratelimit-limit-tokens"),
            ),
            remainingTokens: parseHeaderInt(
              res.headers.get("x-ratelimit-remaining-tokens"),
            ),
            resetTokens: res.headers.get("x-ratelimit-reset-tokens"),
          };
        } catch {
          return null;
        }
      }),
    );

    const valid = probes.filter(
      (p): p is NonNullable<typeof p> =>
        p !== null &&
        (p.remainingRequests !== null || p.remainingTokens !== null),
    );

    if (valid.length === 0) {
      return NextResponse.json(
        { error: "No se pudo obtener la cuota de ninguna clave de Groq" },
        { status: 502 },
      );
    }

    const totalLimitReq = valid.reduce(
      (acc, p) => acc + (p.limitRequests ?? 0),
      0,
    );
    const totalRemReq = valid.reduce(
      (acc, p) => acc + (p.remainingRequests ?? 0),
      0,
    );
    const totalLimitTok = valid.reduce(
      (acc, p) => acc + (p.limitTokens ?? 0),
      0,
    );
    const totalRemTok = valid.reduce(
      (acc, p) => acc + (p.remainingTokens ?? 0),
      0,
    );
    const activeKeys = valid.filter(
      (p) => (p.remainingRequests ?? 0) > 0,
    ).length;

    const snapshot: QuotaSnapshot = {
      model,
      totalKeys: keys.length,
      activeKeys,
      limitRequests: totalLimitReq,
      remainingRequests: totalRemReq,
      resetRequests: valid[0]?.resetRequests ?? null,
      limitTokens: totalLimitTok,
      remainingTokens: totalRemTok,
      resetTokens: valid[0]?.resetTokens ?? null,
      updatedAt: new Date().toISOString(),
    };

    cachedSnapshot = {
      data: snapshot,
      expiresAt: now + CACHE_TTL_MS,
    };

    return NextResponse.json(snapshot, {
      headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json(
      { error: "No se pudo consultar la cuota de Groq" },
      { status: 502 },
    );
  }
}
