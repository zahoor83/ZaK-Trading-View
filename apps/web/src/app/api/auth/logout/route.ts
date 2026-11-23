import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";
import {
  LEGACY_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
} from "@/lib/server/session-cookie";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function clearSessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: SESSION_COOKIE_NAME,
    value: "",
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    expires: new Date(0),
    maxAge: 0,
  });
}

function clearLegacySessionCookie(response: NextResponse): void {
  response.cookies.set({
    name: LEGACY_SESSION_COOKIE_NAME,
    value: "",
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    expires: new Date(0),
    maxAge: 0,
  });
}

function responseWithStatus(status: number): NextResponse {
  const response = NextResponse.json(
    { detail: status === 503 ? "Authentication service unavailable." : "Logout could not be completed." },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        Pragma: "no-cache",
        "Referrer-Policy": "no-referrer",
      },
    },
  );

  return response;
}

function parsePublicOrigin(value: string): URL | null {
  try {
    const parsed = new URL(value);
    const localHost = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);

    if (
      (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && localHost)) ||
      parsed.username ||
      parsed.password ||
      parsed.pathname !== "/" ||
      parsed.search ||
      parsed.hash
    ) {
      return null;
    }

    return parsed;
  } catch {
    return null;
  }
}

function isValidRevokeResponse(value: unknown): boolean {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }

  const data = value as Record<string, unknown>;

  return (
    Object.keys(data).length === 1 &&
    Object.hasOwn(data, "revoked") &&
    typeof data.revoked === "boolean"
  );
}

export async function POST(request: Request): Promise<NextResponse> {
  if (process.env.APP_ENV?.trim().toLowerCase() !== "development") {
    return responseWithStatus(503);
  }

  const configuredOrigin = process.env.APP_PUBLIC_URL;
  if (!configuredOrigin) {
    return responseWithStatus(503);
  }

  const publicUrl = parsePublicOrigin(configuredOrigin);
  if (!publicUrl) {
    return responseWithStatus(503);
  }

  if (request.headers.get("origin") !== publicUrl.origin) {
    return NextResponse.json(
      { detail: "Request origin rejected." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin") {
    return NextResponse.json(
      { detail: "Request origin rejected." },
      { status: 403, headers: { "Cache-Control": "no-store" } },
    );
  }

  let requestUrl: URL;
  try {
    requestUrl = new URL(request.url);
  } catch {
    return responseWithStatus(503);
  }

  if (
    requestUrl.origin !== publicUrl.origin ||
    requestUrl.pathname !== "/api/auth/logout" ||
    requestUrl.search ||
    requestUrl.hash
  ) {
    return responseWithStatus(403);
  }

  const token = request.headers.get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .filter((part) => part.startsWith(`${SESSION_COOKIE_NAME}=`));

  const matchingCookies = token ?? [];

  if (matchingCookies.length > 1) {
    return responseWithStatus(400);
  }

  const rawToken = matchingCookies.length === 1
    ? matchingCookies[0].slice(SESSION_COOKIE_NAME.length + 1)
    : "";

  if (rawToken && (
    rawToken.length < 32 ||
    rawToken.length > 512 ||
    !/^[\x21-\x7E]+$/.test(rawToken)
  )) {
    return responseWithStatus(400);
  }

  if (rawToken) {
    try {
      const upstream = await internalApiFetch(
        "/internal/v1/auth/session/revoke",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ session_token: rawToken }),
        },
      );

      if (upstream.status !== 200) {
        return responseWithStatus(503);
      }

      const payload: unknown = await upstream.json();

      if (!isValidRevokeResponse(payload)) {
        return responseWithStatus(503);
      }
    } catch {
      return responseWithStatus(503);
    }
  }

  const response = NextResponse.redirect(
    new URL("/login", publicUrl.origin),
    303,
  );

  clearSessionCookie(response);
  clearLegacySessionCookie(response);
  response.headers.set("Cache-Control", "no-store");
  response.headers.set("Pragma", "no-cache");
  response.headers.set("Referrer-Policy", "no-referrer");

  return response;
}