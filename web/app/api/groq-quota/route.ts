import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

interface QuotaSnapshot {
  model: string;
  limitRequests: number | null;
  remainingRequests: number | null;
  resetRequests: string | null;
  limitTokens: number | null;
  remainingTokens: number | null;
  resetTokens: string | null;
  updatedAt: string;
}

let cachedSnapshot: { data: QuotaSnapshot; expiresAt: number } | null = null;
const CACHE_TTL_MS = 45_000;

function parseHeaderInt(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function GET(request: NextRequest) {
  const groqKey = process.env.GROQ_API_KEY?.trim();
  const model = process.env.GROQ_LLM_MODEL?.trim() || "qwen/qwen3.8-27b";

  if (!groqKey) {
    return NextResponse.json(
      { error: "Falta configurar GROQ_API_KEY en el servidor web" },
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
    const res = await fetch("https://api.groq.com/openai/v1/chat/completions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${groqKey}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        model,
        messages: [{ role: "user", content: "." }],
        max_completion_tokens: 1,
      }),
      cache: "no-store",
    });

    const limitRequests = parseHeaderInt(
      res.headers.get("x-ratelimit-limit-requests"),
    );
    const remainingRequests = parseHeaderInt(
      res.headers.get("x-ratelimit-remaining-requests"),
    );
    const resetRequests = res.headers.get("x-ratelimit-reset-requests");

    const limitTokens = parseHeaderInt(
      res.headers.get("x-ratelimit-limit-tokens"),
    );
    const remainingTokens = parseHeaderInt(
      res.headers.get("x-ratelimit-remaining-tokens"),
    );
    const resetTokens = res.headers.get("x-ratelimit-reset-tokens");

    if (!res.ok && remainingRequests === null && remainingTokens === null) {
      return NextResponse.json(
        { error: `Groq respondió HTTP ${res.status}` },
        { status: res.status },
      );
    }

    const snapshot: QuotaSnapshot = {
      model,
      limitRequests,
      remainingRequests,
      resetRequests,
      limitTokens,
      remainingTokens,
      resetTokens,
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
