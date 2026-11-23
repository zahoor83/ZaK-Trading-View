import "server-only";

import { cookies } from "next/headers";

import { internalApiFetch } from "@/lib/server/internal-api";
import {
  readSingleSessionToken,
  SESSION_COOKIE_NAME,
} from "@/lib/server/session-cookie";

export type AuthenticatedUser = {
  id: string;
  email: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function validateSessionResponse(
  value: unknown,
): AuthenticatedUser | null {
  if (!isRecord(value)) {
    throw new Error("Invalid authentication service response.");
  }

  const keys = ["authenticated", "user", "session"];

  if (
    Object.keys(value).length !== keys.length ||
    !keys.every((key) => Object.hasOwn(value, key)) ||
    value.authenticated !== true ||
    !isRecord(value.user) ||
    !isRecord(value.session)
  ) {
    throw new Error("Invalid authentication service response.");
  }

  const user = value.user;
  const session = value.session;

  if (
    Object.keys(user).length !== 2 ||
    !Object.hasOwn(user, "id") ||
    !Object.hasOwn(user, "email") ||
    Object.keys(session).length !== 1 ||
    !Object.hasOwn(session, "expires_at")
  ) {
    throw new Error("Invalid authentication service response.");
  }

  if (
    typeof user.id !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(user.id) ||
    typeof user.email !== "string" ||
    user.email.length < 3 ||
    user.email.length > 320 ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(user.email) ||
    typeof session.expires_at !== "string" ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(session.expires_at)
  ) {
    throw new Error("Invalid authentication service response.");
  }

  const expiry = Date.parse(session.expires_at);

  if (!Number.isFinite(expiry) || expiry <= Date.now()) {
    return null;
  }

  return { id: user.id, email: user.email };
}

export async function getAuthenticatedUser(): Promise<AuthenticatedUser | null> {
  const cookieStore = await cookies();
  const token = readSingleSessionToken(
    cookieStore.getAll(SESSION_COOKIE_NAME),
  );

  if (
    !token ||
    token.length < 32 ||
    token.length > 512 ||
    !/^[\x21-\x7E]+$/.test(token)
  ) {
    return null;
  }

  const response = await internalApiFetch(
    "/internal/v1/auth/session/validate",
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session_token: token }),
    },
  );

  if (response.status === 401) {
    return null;
  }

  if (response.status !== 200) {
    throw new Error("Authentication service unavailable.");
  }

  let payload: unknown;

  try {
    payload = await response.json();
  } catch {
    throw new Error("Invalid authentication service response.");
  }

  return validateSessionResponse(payload);
}