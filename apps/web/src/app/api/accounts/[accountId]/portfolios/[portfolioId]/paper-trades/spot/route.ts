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

const UUID_PATTERN =
  /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

const COOKIE_OCTET_PATTERN =
  /^[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]+$/;

const QUANTITY_PATTERN =
  /^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,36})?$/;

type JsonRecord = Record<string, unknown>;

function jsonError(status: number, detail: string): NextResponse {
  return NextResponse.json(
    { detail },
    { status, headers: NO_STORE_HEADERS },
  );
}

function isRecord(value: unknown): value is JsonRecord {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function validQuantity(value: unknown): value is string {
  if (
    typeof value !== "string" ||
    value.length > 64 ||
    !QUANTITY_PATTERN.test(value)
  ) {
    return false;
  }

  if (/^0(?:\.0+)?$/.test(value)) {
    return false;
  }

  const integerPart = value.split(".")[0];

  return integerPart.length <= 36;
}

function validateTradeBody(value: unknown): JsonRecord | null {
  if (!isRecord(value)) {
    return null;
  }

  const allowedKeys = new Set([
    "symbol",
    "side",
    "quantity",
    "position_id",
  ]);

  if (Object.keys(value).some((key) => !allowedKeys.has(key))) {
    return null;
  }

  if (
    typeof value.symbol !== "string" ||
    value.symbol.length < 1 ||
    value.symbol.length > 64 ||
    !/^[A-Z0-9_-]{1,64}$/.test(value.symbol) ||
    (value.side !== "buy" && value.side !== "sell") ||
    !validQuantity(value.quantity)
  ) {
    return null;
  }

  const positionId = value.position_id;

  if (value.side === "buy") {
    if (positionId !== undefined && positionId !== null) {
      return null;
    }
  } else {
    if (
      typeof positionId !== "string" ||
      !UUID_V4_PATTERN.test(positionId.toLowerCase())
    ) {
      return null;
    }
  }

  return {
    symbol: value.symbol,
    side: value.side,
    quantity: value.quantity,
    position_id:
      typeof positionId === "string"
        ? positionId.toLowerCase()
        : null,
  };
}

function safeUpstreamDetail(value: unknown): string {
  if (
    isRecord(value) &&
    typeof value.detail === "string" &&
    value.detail.length > 0 &&
    value.detail.length <= 500
  ) {
    return value.detail;
  }

  return "Spot paper trade could not be completed.";
}

export async function POST(request: Request): Promise<NextResponse> {
  const publicUrl = parseAppPublicOrigin(process.env.APP_PUBLIC_URL, process.env.APP_ENV);

  if (!publicUrl) {
    return jsonError(503, "Paper Trading service is unavailable.");
  }

  if (request.headers.get("origin") !== publicUrl.origin) {
    return jsonError(403, "Request origin rejected.");
  }

  const fetchSite = request.headers.get("sec-fetch-site");

  if (fetchSite !== null && fetchSite !== "same-origin") {
    return jsonError(403, "Request origin rejected.");
  }

  let requestUrl: URL;

  try {
    requestUrl = new URL(request.url);
  } catch {
    return jsonError(400, "Invalid request URL.");
  }

  const routeMatch = requestUrl.pathname.match(
    /^\/api\/accounts\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/portfolios\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/paper-trades\/spot$/,
  );

  if (
    requestUrl.origin !== publicUrl.origin ||
    !routeMatch ||
    requestUrl.search ||
    requestUrl.hash
  ) {
    return jsonError(400, "Invalid Spot paper-trading request.");
  }

  const contentType = request.headers
    .get("content-type")
    ?.split(";")[0]
    .trim()
    .toLowerCase();

  if (contentType !== "application/json") {
    return jsonError(415, "Content-Type must be application/json.");
  }

  const contentLength = request.headers.get("content-length");

  if (
    contentLength !== null &&
    (!/^[0-9]+$/.test(contentLength) ||
      Number(contentLength) > 4096)
  ) {
    return jsonError(413, "Request body is too large.");
  }

  let rawBody: string;

  try {
    rawBody = await request.text();
  } catch {
    return jsonError(400, "Invalid request body.");
  }

  if (rawBody.length === 0 || rawBody.length > 4096) {
    return jsonError(400, "Invalid request body.");
  }

  let parsedBody: unknown;

  try {
    parsedBody = JSON.parse(rawBody);
  } catch {
    return jsonError(400, "Invalid JSON request body.");
  }

  const tradeBody = validateTradeBody(parsedBody);

  if (!tradeBody) {
    return jsonError(422, "Spot trade request validation failed.");
  }

  const idempotencyKey = request.headers.get("idempotency-key");

  if (
    idempotencyKey === null ||
    !UUID_V4_PATTERN.test(idempotencyKey)
  ) {
    return jsonError(
      400,
      "Idempotency-Key must be a canonical lowercase UUID v4.",
    );
  }

  let authenticatedUser: Awaited<
    ReturnType<typeof getAuthenticatedUser>
  >;

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

  if (
    !sessionToken ||
    !COOKIE_OCTET_PATTERN.test(sessionToken)
  ) {
    return jsonError(401, "Authentication required.");
  }

  const accountId = routeMatch[1].toLowerCase();
  const portfolioId = routeMatch[2].toLowerCase();

  const upstreamPath =
    `/api/v1/accounts/${accountId}` +
    `/portfolios/${portfolioId}/paper-trades/spot`;

  try {
    const upstream = await internalApiFetch(upstreamPath, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "Idempotency-Key": idempotencyKey,
        Cookie: `zak_session=${sessionToken}`,
      },
      body: JSON.stringify(tradeBody),
    });

    let upstreamPayload: unknown;

    try {
      upstreamPayload = await upstream.json();
    } catch {
      return jsonError(
        503,
        "Paper Trading service returned an invalid response.",
      );
    }

    if (upstream.status === 200) {
      if (!isRecord(upstreamPayload)) {
        return jsonError(
          503,
          "Paper Trading service returned an invalid response.",
        );
      }

      return NextResponse.json(upstreamPayload, {
        status: 200,
        headers: NO_STORE_HEADERS,
      });
    }

    const mappedStatus = [
      400,
      401,
      403,
      404,
      409,
      422,
    ].includes(upstream.status)
      ? upstream.status
      : 503;

    return jsonError(
      mappedStatus,
      mappedStatus === 503
        ? "Paper Trading service is temporarily unavailable."
        : safeUpstreamDetail(upstreamPayload),
    );
  } catch {
    return jsonError(503, "Paper Trading service is unavailable.");
  }
}
