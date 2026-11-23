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

function jsonError(status: number, detail: string): NextResponse {
  return NextResponse.json(
    { detail },
    { status, headers: NO_STORE_HEADERS },
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
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

  return "Manual Top-up request could not be completed.";
}

function validateRequestBody(value: unknown): { confirmed: true } | null {
  if (!isRecord(value)) {
    return null;
  }

  const keys = Object.keys(value);

  if (
    keys.length !== 1 ||
    keys[0] !== "confirmed" ||
    value.confirmed !== true
  ) {
    return null;
  }

  return { confirmed: true };
}

export async function POST(request: Request): Promise<NextResponse> {
  const publicUrl = parseAppPublicOrigin(
    process.env.APP_PUBLIC_URL,
    process.env.APP_ENV,
  );

  if (!publicUrl) {
    return jsonError(503, "Application origin is not configured.");
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
    /^\/api\/accounts\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/portfolios\/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\/manual-top-up$/,
  );

  if (
    requestUrl.origin !== publicUrl.origin ||
    !routeMatch ||
    requestUrl.search ||
    requestUrl.hash
  ) {
    return jsonError(400, "Invalid Manual Top-up request.");
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

  const operationBody = validateRequestBody(parsedBody);

  if (!operationBody) {
    return jsonError(422, "Manual Top-up confirmation is invalid.");
  }

  const idempotencyKey = request.headers.get("idempotency-key");

  if (
    idempotencyKey === null ||
    !UUID_V4_PATTERN.test(idempotencyKey)
  ) {
    return jsonError(
      400,
      "Idempotency-Key must be a canonical lowercase UUIDv4.",
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
    `/portfolios/${portfolioId}/manual-top-up`;

  try {
    const upstream = await internalApiFetch(upstreamPath, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "Idempotency-Key": idempotencyKey,
        Cookie: `zak_session=${sessionToken}`,
      },
      body: JSON.stringify(operationBody),
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
