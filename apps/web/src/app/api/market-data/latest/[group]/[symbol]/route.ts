import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";

export const dynamic = "force-dynamic";

type RouteContext = { params: Promise<{ group: string; symbol: string }> };

export async function GET(
  _request: Request,
  context: RouteContext,
): Promise<NextResponse> {
  const { group, symbol } = await context.params;

  if (
    !["spot", "futures_mark_price"].includes(group) ||
    !/^[A-Z0-9]{2,30}$/.test(symbol)
  ) {
    return NextResponse.json(
      { detail: "Unsupported market group or symbol." },
      { status: 404 },
    );
  }

  const path =
    `/api/v1/market-data/latest/${encodeURIComponent(group)}/${encodeURIComponent(symbol)}`;

  try {
    const response = await internalApiFetch(path, {
      method: "GET",
      signal: AbortSignal.timeout(5000),
    });

    const body = await response.text();

    return new NextResponse(body, {
      status: response.status,
      headers: {
        "content-type": "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  } catch {
    return NextResponse.json(
      { detail: "Market-data service is unavailable." },
      { status: 503 },
    );
  }
}