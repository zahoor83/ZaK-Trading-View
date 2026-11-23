import { cookies } from "next/headers";
import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";
import { parseAppPublicOrigin } from "@/lib/server/internal-api-validation";
import { getAuthenticatedUser } from "@/lib/server/session";
import {
  readSingleSessionToken,
  SESSION_COOKIE_NAME,
} from "@/lib/server/session-cookie";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const NO_STORE_HEADERS = {
  "Cache-Control": "no-store",
  Pragma: "no-cache",
  "Referrer-Policy": "no-referrer",
};

const COOKIE_OCTET_PATTERN =
  /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$/;

type JsonRecord = Record<string, unknown>;

function jsonError(status: number, detail: string): NextResponse {
  return NextResponse.json(
    { detail },
    { status, headers: NO_STORE_HEADERS },
  );
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isBootstrapPayload(value: unknown): value is JsonRecord {
  if (!isRecord(value) || !isRecord(value.account)) return false;
  if (!Array.isArray(value.portfolios) || value.portfolios.length !== 2) {
    return false;
  }

  const markets = new Set<string>();
  for (const portfolio of value.portfolios) {
    if (!isRecord(portfolio) || !isRecord(portfolio.session) ||
        !isRecord(portfolio.ledger) || !Array.isArray(portfolio.positions)) {
      return false;
    }
    if (portfolio.market_type !== "spot" && portfolio.market_type !== "futures") {
      return false;
    }
    markets.add(portfolio.market_type);
  }

  return markets.size === 2;
}

export async function GET(request: Request): Promise<NextResponse> {
  const publicUrl = parseAppPublicOrigin(
    process.env.APP_PUBLIC_URL,
    process.env.APP_ENV,
  );

  if (!publicUrl) {
    return jsonError(503, "Paper Trading service is unavailable.");
  }

  let requestUrl: URL;
  try {
    requestUrl = new URL(request.url);
  } catch {
    return jsonError(400, "Invalid request URL.");
  }

  if (
    requestUrl.origin !== publicUrl.origin ||
    requestUrl.pathname !== "/api/paper-trading/bootstrap" ||
    requestUrl.search ||
    requestUrl.hash
  ) {
    return jsonError(400, "Invalid Paper Trading bootstrap request.");
  }

  const origin = request.headers.get("origin");
  if (origin !== null && origin !== publicUrl.origin) {
    return jsonError(403, "Request origin rejected.");
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite !== null && fetchSite !== "same-origin" && fetchSite !== "none") {
    return jsonError(403, "Request origin rejected.");
  }

  let authenticatedUser: Awaited<ReturnType<typeof getAuthenticatedUser>>;
  try {
    authenticatedUser = await getAuthenticatedUser();
  } catch {
    return jsonError(503, "Authentication service is unavailable.");
  }

  if (!authenticatedUser) {
    return jsonError(401, "Authentication required.");
  }

  let sessionToken: string | null;
  try {
    const cookieStore = await cookies();
    sessionToken = readSingleSessionToken(
      cookieStore.getAll(SESSION_COOKIE_NAME),
    );
  } catch {
    return jsonError(401, "Authentication required.");
  }

  if (!sessionToken || !COOKIE_OCTET_PATTERN.test(sessionToken)) {
    return jsonError(401, "Authentication required.");
  }

  try {
    const upstream = await internalApiFetch(
      "/api/v1/paper-trading/bootstrap",
      {
        method: "GET",
        headers: {
          Cookie: `zak_session=${sessionToken}`,
          Accept: "application/json",
        },
      },
    );

    let payload: unknown;
    try {
      payload = await upstream.json();
    } catch {
      return jsonError(503, "Paper Trading service returned an invalid response.");
    }

    if (upstream.status === 200) {
      if (!isBootstrapPayload(payload)) {
        return jsonError(503, "Paper Trading service returned an invalid response.");
      }
      return NextResponse.json(payload, {
        status: 200,
        headers: NO_STORE_HEADERS,
      });
    }

    if ([401, 403, 404, 409].includes(upstream.status)) {
      const detailByStatus: Record<number, string> = {
        401: "Authentication required.",
        403: "Paper Trading access is not permitted.",
        404: "Paper Trading account was not found.",
        409: "Paper Trading account state requires review.",
      };
      return jsonError(upstream.status, detailByStatus[upstream.status]);
    }

    return jsonError(503, "Paper Trading service is temporarily unavailable.");
  } catch {
    return jsonError(503, "Paper Trading service is unavailable.");
  }
}
