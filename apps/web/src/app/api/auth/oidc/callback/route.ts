import { createHash } from "node:crypto";
import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";
import {
  LEGACY_SESSION_COOKIE_NAME,
  SESSION_COOKIE_NAME,
} from "@/lib/server/session-cookie";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const BROWSER_COOKIE_PREFIX = "__Host-zak_oidc_";
const SESSION_TTL_SECONDS = 12 * 60 * 60;


function errorResponse(status: number, message: string): NextResponse {
  return NextResponse.json(
    { detail: message },
    {
      status,
      headers: {
        "Cache-Control": "no-store",
        Pragma: "no-cache",
        "Referrer-Policy": "no-referrer",
      },
    },
  );
}

function parsePublicOrigin(value: string): URL | null {
  try {
    const parsed = new URL(value);
    const localHost = ["localhost", "127.0.0.1", "[::1]"].includes(
      parsed.hostname,
    );

    if (
      (parsed.protocol !== "https:" &&
        !(parsed.protocol === "http:" && localHost)) ||
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

function browserCookieName(state: string): string {
  const hash = createHash("sha256")
    .update(Buffer.from("state", "ascii"))
    .update(Buffer.from([0]))
    .update(Buffer.from(state, "ascii"))
    .digest("hex");

  return BROWSER_COOKIE_PREFIX + hash.slice(0, 24);
}

function clearBrowserCookie(
  response: NextResponse,
  cookieName: string,
): void {
  response.cookies.set({
    name: cookieName,
    value: "",
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: 0,
    expires: new Date(0),
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return (
    typeof value === "object" &&
    value !== null &&
    !Array.isArray(value)
  );
}

function validateCallbackPayload(input: unknown): {
  token: string;
  expiresAt: Date;
} {
  if (!isRecord(input)) {
    throw new Error("Invalid callback response.");
  }

  if (
    Object.keys(input).length !== 2 ||
    !Object.hasOwn(input, "session") ||
    !Object.hasOwn(input, "user")
  ) {
    throw new Error("Invalid callback response.");
  }

  const session = input.session;
  const user = input.user;

  if (!isRecord(session) || !isRecord(user)) {
    throw new Error("Invalid callback response.");
  }

  if (
    Object.keys(session).length !== 2 ||
    !Object.hasOwn(session, "token") ||
    !Object.hasOwn(session, "expires_at") ||
    Object.keys(user).length !== 2 ||
    !Object.hasOwn(user, "id") ||
    !Object.hasOwn(user, "email")
  ) {
    throw new Error("Invalid callback response.");
  }

  const token = session.token;
  const expiresAtValue = session.expires_at;
  const userId = user.id;
  const email = user.email;

  if (
    typeof token !== "string" ||
    token.length < 32 ||
    token.length > 512 ||
    !/^[\x21-\x7E]+$/.test(token) ||
    typeof expiresAtValue !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(expiresAtValue) ||
    typeof userId !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      userId,
    ) ||
    typeof email !== "string" ||
    email.length < 3 ||
    email.length > 320 ||
    !email.includes("@") ||
    /[\r\n]/.test(email)
  ) {
    throw new Error("Invalid callback response.");
  }

  const expiryMilliseconds = Date.parse(expiresAtValue);
  const now = Date.now();
  const remainingMilliseconds = expiryMilliseconds - now;

  if (
    !Number.isFinite(expiryMilliseconds) ||
    remainingMilliseconds <= 0 ||
    remainingMilliseconds > SESSION_TTL_SECONDS * 1000 + 30_000
  ) {
    throw new Error("Invalid session expiry.");
  }

  return {
    token,
    expiresAt: new Date(expiryMilliseconds),
  };
}

export async function GET(request: Request): Promise<NextResponse> {
  const appEnv = process.env.APP_ENV?.trim().toLowerCase();
  const publicUrlValue = process.env.APP_PUBLIC_URL;
  const callbackUrlValue = process.env.AUTH0_CALLBACK_URL;

  if (
    appEnv !== "development" ||
    !publicUrlValue ||
    !callbackUrlValue
  ) {
    return errorResponse(
      503,
      "Authentication service unavailable.",
    );
  }

  const publicUrl = parsePublicOrigin(publicUrlValue);

  if (!publicUrl) {
    return errorResponse(
      503,
      "Authentication service unavailable.",
    );
  }

  let callbackUrl: URL;
  let requestUrl: URL;

  try {
    callbackUrl = new URL(callbackUrlValue);
    requestUrl = new URL(request.url);
  } catch {
    return errorResponse(
      503,
      "Authentication service unavailable.",
    );
  }

  const expectedCallbackPath = "/api/auth/oidc/callback";

  if (
    callbackUrl.origin !== publicUrl.origin ||
    callbackUrl.pathname !== expectedCallbackPath ||
    callbackUrl.search ||
    callbackUrl.hash ||
    callbackUrl.username ||
    callbackUrl.password ||
    requestUrl.origin !== publicUrl.origin ||
    requestUrl.pathname !== expectedCallbackPath
  ) {
    return errorResponse(
      503,
      "Authentication service unavailable.",
    );
  }

  const params = requestUrl.searchParams;
  const states = params.getAll("state");
  const codes = params.getAll("code");
  const errors = params.getAll("error");

  if (
    states.length !== 1 ||
    !/^[A-Za-z0-9_-]{43,128}$/.test(states[0])
  ) {
    return errorResponse(
      400,
      "Authentication could not be completed.",
    );
  }

  const state = states[0];
  const cookieName = browserCookieName(state);
  const cookieHeader = request.headers.get("cookie");

  // Accept exactly one cookie for this state-derived name.
  // Reject duplicate names rather than choosing an ambiguous value.
  const matchingCookies = cookieHeader
    ? cookieHeader
        .split(";")
        .map((part) => part.trim())
        .filter((part) => part.startsWith(`${cookieName}=`))
    : [];

  const bindingValue =
    matchingCookies.length === 1
      ? matchingCookies[0].slice(cookieName.length + 1)
      : null;

  if (
    matchingCookies.length !== 1 ||
    !bindingValue ||
    !/^[A-Za-z0-9_-]{43}$/.test(bindingValue)
  ) {
    return errorResponse(
      400,
      "Authentication could not be completed.",
    );
  }

  if (
    errors.length > 1 ||
    codes.length > 1 ||
    (errors.length === 1 && codes.length !== 0) ||
    (errors.length === 0 && codes.length !== 1)
  ) {
    const response = errorResponse(
      400,
      "Authentication could not be completed.",
    );
    clearBrowserCookie(response, cookieName);
    return response;
  }

  if (errors.length === 1) {
    const response = errorResponse(
      400,
      "Authentication was not completed.",
    );
    clearBrowserCookie(response, cookieName);
    return response;
  }

  const authorizationCode = codes[0];

  if (
    typeof authorizationCode !== "string" ||
    authorizationCode.length < 1 ||
    authorizationCode.length > 8192 ||
    /[\r\n]/.test(authorizationCode)
  ) {
    const response = errorResponse(
      400,
      "Authentication could not be completed.",
    );
    clearBrowserCookie(response, cookieName);
    return response;
  }

  try {
    const upstream = await internalApiFetch(
      "/internal/v1/auth/oidc/callback",
      {
        method: "POST",
        headers: {
          "content-type": "application/json",
        },
        body: JSON.stringify({
          state,
          authorization_code: authorizationCode,
          browser_binding: bindingValue,
        }),
      },
    );

    if (upstream.status !== 200) {
      const response = errorResponse(
        upstream.status === 401 ? 401 : 503,
        "Authentication could not be completed.",
      );
      clearBrowserCookie(response, cookieName);
      return response;
    }

    const payload: unknown = await upstream.json();
    const validated = validateCallbackPayload(payload);

    const remainingSeconds = Math.floor(
      (validated.expiresAt.getTime() - Date.now()) / 1000,
    );

    if (remainingSeconds < 1) {
      throw new Error("Session expired.");
    }

    const response = NextResponse.redirect(
      new URL("/", publicUrl.origin),
      303,
    );

    response.cookies.set({
      name: SESSION_COOKIE_NAME,
      value: validated.token,
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      expires: validated.expiresAt,
      maxAge: remainingSeconds,
    });

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

    clearBrowserCookie(response, cookieName);

    response.headers.set("Cache-Control", "no-store");
    response.headers.set("Pragma", "no-cache");
    response.headers.set("Referrer-Policy", "no-referrer");

    return response;
  } catch {
    const response = errorResponse(
      503,
      "Authentication service unavailable.",
    );
    clearBrowserCookie(response, cookieName);
    return response;
  }
}