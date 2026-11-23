import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";

export const dynamic = "force-dynamic";

export async function GET(): Promise<NextResponse> {
  try {
    const response = await internalApiFetch(
      "/api/v1/market-data/health",
      {
        method: "GET",
        signal: AbortSignal.timeout(5000),
      },
    );

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