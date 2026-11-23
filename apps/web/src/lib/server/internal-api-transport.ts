import "server-only";

export interface InternalServiceTransport {
  fetch(url: URL, init: RequestInit): Promise<Response>;
}

export interface InternalServiceTransportEnvironment {
  APP_ENV?: string;
  OIDC_INTERNAL_SERVICE_SECRET?: string;
}

export class InternalServiceTransportConfigurationError extends Error {
  constructor() {
    super("Internal service transport is unavailable.");
    this.name = "InternalServiceTransportConfigurationError";
  }
}

const LOCAL_SECRET_HEADER = "X-Zak-Internal-Service-Secret";
const MIN_SECRET_LENGTH = 32;

type TransportRegistry = typeof globalThis & {
  __zakInternalServiceTransport?: InternalServiceTransport;
};

function isTransport(
  value: unknown,
): value is InternalServiceTransport {
  if (typeof value !== "object" || value === null) {
    return false;
  }

  return (
    "fetch" in value &&
    typeof value.fetch === "function"
  );
}

export function createDevelopmentInternalServiceTransport(
  secret: string | undefined,
): InternalServiceTransport {
  if (
    typeof secret !== "string" ||
    secret.length < MIN_SECRET_LENGTH
  ) {
    throw new InternalServiceTransportConfigurationError();
  }

  return Object.freeze({
    async fetch(url: URL, init: RequestInit): Promise<Response> {
      const headers = new Headers(init.headers);
      headers.set(LOCAL_SECRET_HEADER, secret);

      return fetch(url, {
        ...init,
        headers,
      });
    },
  });
}

/**
 * Resolve the transport for an explicitly declared environment.
 * Production never falls back to the local shared-secret adapter.
 */
export function resolveInternalServiceTransport(
  environment: InternalServiceTransportEnvironment,
  injectedProductionTransport?: InternalServiceTransport,
): InternalServiceTransport {
  const appEnv = environment.APP_ENV?.trim().toLowerCase();

  if (appEnv === "development") {
    if (injectedProductionTransport !== undefined) {
      throw new InternalServiceTransportConfigurationError();
    }

    return createDevelopmentInternalServiceTransport(
      environment.OIDC_INTERNAL_SERVICE_SECRET,
    );
  }

  if (appEnv === "production") {
    if (!isTransport(injectedProductionTransport)) {
      throw new InternalServiceTransportConfigurationError();
    }

    return injectedProductionTransport;
  }

  throw new InternalServiceTransportConfigurationError();
}

/**
 * Deployment bootstrap may register its production transport before
 * the Next.js server begins accepting requests.
 */
export function registerProductionInternalServiceTransport(
  transport: InternalServiceTransport,
): void {
  if (process.env.APP_ENV?.trim().toLowerCase() !== "production") {
    throw new InternalServiceTransportConfigurationError();
  }

  if (!isTransport(transport)) {
    throw new InternalServiceTransportConfigurationError();
  }

  const registry = globalThis as TransportRegistry;
  const existing = registry.__zakInternalServiceTransport;

  if (existing !== undefined && existing !== transport) {
    throw new InternalServiceTransportConfigurationError();
  }

  registry.__zakInternalServiceTransport = transport;
}

export function getInternalServiceTransport(): InternalServiceTransport {
  const registry = globalThis as TransportRegistry;

  return resolveInternalServiceTransport(
    {
      APP_ENV: process.env.APP_ENV,
      OIDC_INTERNAL_SERVICE_SECRET:
        process.env.OIDC_INTERNAL_SERVICE_SECRET,
    },
    registry.__zakInternalServiceTransport,
  );
}
