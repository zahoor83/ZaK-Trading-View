import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ group: string; symbol: string }> };

function upstreamUrl(path: string): URL | null {
  const configured = process.env.ZAK_MARKET_DATA_API_BASE_URL;
  if (!configured) return null;

  try {
    const base = new URL(configured);
    if (
      !["http:", "https:"].includes(base.protocol) ||
      base.username ||
      base.password ||
      base.search ||
      base.hash
    ) {
      return null;
    }
    return new URL(path, `${base.origin}${base.pathname.replace(/\/$/, "")}/`);
  } catch {
    return null;
  }
}

export async function GET(_request: Request, context: RouteContext): Promise<NextResponse> {
  const { group, symbol } = await context.params;
  if (
    !["spot", "futures_mark_price"].includes(group) ||
    !/^[A-Z0-9]{2,30}$/.test(symbol)
  ) {
    return NextResponse.json({ detail: "Unsupported market group or symbol." }, { status: 404 });
  }

  const path = `/api/v1/market-data/latest/${encodeURIComponent(group)}/${encodeURIComponent(symbol)}`;
  const url = upstreamUrl(path);
  if (!url) {
    return NextResponse.json(
      { detail: "Market-data API base URL is not configured." },
      { status: 503 },
    );
  }

  try {
    const response = await fetch(url, {
      method: "GET",
      cache: "no-store",
      signal: AbortSignal.timeout(5000),
      headers: { accept: "application/json" },
    });
    const body = await response.text();
    return new NextResponse(body, {
      status: response.status,
      headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
    });
  } catch {
    return NextResponse.json(
      { detail: "Market-data service is unreachable." },
      { status: 503 },
    );
  }
}
