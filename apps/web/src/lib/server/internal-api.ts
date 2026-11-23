/**
 * Server-side API client for the Next.js BFF.
 *
 * Never import this module from a Client Component.
 * Authentication is delegated to an environment-specific transport.
 */

import "server-only";

import { buildInternalApiUrl } from "@/lib/server/internal-api-validation";
import { getInternalServiceTransport } from "@/lib/server/internal-api-transport";

export class InternalApiConfigurationError extends Error {
  constructor() {
    super("Internal API authentication is unavailable.");
    this.name = "InternalApiConfigurationError";
  }
}

export function internalApiUrl(
  path: string,
  method = "GET",
): URL {
  if (typeof window !== "undefined") {
    throw new InternalApiConfigurationError();
  }

  const url = buildInternalApiUrl(
    path,
    method,
    process.env.ZAK_MARKET_DATA_API_BASE_URL,
    process.env.APP_ENV,
  );

  if (!url) {
    throw new InternalApiConfigurationError();
  }

  return url;
}
export async function internalApiFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  if (typeof window !== "undefined") {
    throw new InternalApiConfigurationError();
  }

  const method = init.method ?? "GET";
  const url = internalApiUrl(path, method);
  const transport = getInternalServiceTransport();

  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");

  return transport.fetch(url, {
    ...init,
    headers,
    cache: "no-store",
    redirect: "error",
    signal: init.signal ?? AbortSignal.timeout(5000),
  });
}
