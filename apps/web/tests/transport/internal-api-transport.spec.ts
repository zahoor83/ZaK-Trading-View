import {
  test,
  expect,
} from "@playwright/test";

import {
  createDevelopmentInternalServiceTransport,
  getInternalServiceTransport,
  registerProductionInternalServiceTransport,
  resolveInternalServiceTransport,
  InternalServiceTransportConfigurationError,
  type InternalServiceTransport,
} from "../../src/lib/server/internal-api-transport";

type TransportRegistry = typeof globalThis & {
  __zakInternalServiceTransport?: InternalServiceTransport;
};

const registry = globalThis as TransportRegistry;

const validSecret =
  "local-development-test-secret-at-least-32-characters";

const originalAppEnv = process.env.APP_ENV;
const originalSecret = process.env.OIDC_INTERNAL_SERVICE_SECRET;

function clearRegisteredTransport(): void {
  delete registry.__zakInternalServiceTransport;
}

function restoreEnvironment(): void {
  if (originalAppEnv === undefined) {
    delete process.env.APP_ENV;
  } else {
    process.env.APP_ENV = originalAppEnv;
  }

  if (originalSecret === undefined) {
    delete process.env.OIDC_INTERNAL_SERVICE_SECRET;
  } else {
    process.env.OIDC_INTERNAL_SERVICE_SECRET = originalSecret;
  }
}

test.beforeEach(() => {
  clearRegisteredTransport();
  process.env.APP_ENV = "development";
  process.env.OIDC_INTERNAL_SERVICE_SECRET = validSecret;
});

test.afterEach(() => {
  clearRegisteredTransport();
  restoreEnvironment();
});

test("development transport accepts a valid secret", () => {
  const transport =
    createDevelopmentInternalServiceTransport(validSecret);

  expect(transport).toBeDefined();
  expect(typeof transport.fetch).toBe("function");
});

test("development transport rejects a missing secret", () => {
  expect(() =>
    createDevelopmentInternalServiceTransport(undefined),
  ).toThrow(InternalServiceTransportConfigurationError);
});

test("development transport rejects a short secret", () => {
  expect(() =>
    createDevelopmentInternalServiceTransport("short-secret"),
  ).toThrow(InternalServiceTransportConfigurationError);
});

test("development transport injects the internal secret header", async () => {
  const transport =
    createDevelopmentInternalServiceTransport(validSecret);

  const originalFetch = globalThis.fetch;

  let capturedUrl: URL | undefined;
  let capturedHeaders: Headers | undefined;
  let capturedCache: RequestCache | undefined;

  globalThis.fetch = (async (
    input: RequestInfo | URL,
    init?: RequestInit,
  ): Promise<Response> => {
    capturedUrl = input instanceof URL
      ? input
      : new URL(String(input));

    capturedHeaders = new Headers(init?.headers);
    capturedCache = init?.cache;

    return new Response("ok", { status: 200 });
  }) as typeof fetch;

  try {
    const response = await transport.fetch(
      new URL("http://127.0.0.1:8000/internal/test"),
      {
        method: "GET",
        headers: {
          "x-existing-header": "preserved",
        },
        cache: "no-store",
      },
    );

    expect(response.status).toBe(200);
    expect(capturedUrl?.pathname).toBe("/internal/test");
    expect(capturedHeaders?.get("X-Zak-Internal-Service-Secret"))
      .toBe(validSecret);
    expect(capturedHeaders?.get("x-existing-header"))
      .toBe("preserved");
    expect(capturedCache).toBe("no-store");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("production rejects missing adapter even when local secret exists", () => {
  expect(() =>
    resolveInternalServiceTransport(
      {
        APP_ENV: "production",
        OIDC_INTERNAL_SERVICE_SECRET: validSecret,
      },
    ),
  ).toThrow(InternalServiceTransportConfigurationError);
});

test("production accepts a valid injected transport", () => {
  const productionTransport: InternalServiceTransport = {
    async fetch(): Promise<Response> {
      return new Response("production", { status: 200 });
    },
  };

  const resolved = resolveInternalServiceTransport(
    {
      APP_ENV: "production",
      OIDC_INTERNAL_SERVICE_SECRET: validSecret,
    },
    productionTransport,
  );

  expect(resolved).toBe(productionTransport);
});

test("production rejects an invalid injected transport", () => {
  expect(() =>
    resolveInternalServiceTransport(
      { APP_ENV: "production" },
      {} as InternalServiceTransport,
    ),
  ).toThrow(InternalServiceTransportConfigurationError);
});

test("missing and unsupported environments fail closed", () => {
  for (const environment of [
    {},
    { APP_ENV: "" },
    { APP_ENV: "staging" },
    { APP_ENV: "test" },
    { APP_ENV: "unknown" },
  ]) {
    expect(() =>
      resolveInternalServiceTransport(environment),
    ).toThrow(InternalServiceTransportConfigurationError);
  }
});

test("development rejects a production transport injection", () => {
  const injected: InternalServiceTransport = {
    async fetch(): Promise<Response> {
      return new Response(null, { status: 204 });
    },
  };

  expect(() =>
    resolveInternalServiceTransport(
      {
        APP_ENV: "development",
        OIDC_INTERNAL_SERVICE_SECRET: validSecret,
      },
      injected,
    ),
  ).toThrow(InternalServiceTransportConfigurationError);
});

test("production registration requires production environment and rejects conflicts", () => {
  const first: InternalServiceTransport = {
    async fetch(): Promise<Response> {
      return new Response("first", { status: 200 });
    },
  };

  const second: InternalServiceTransport = {
    async fetch(): Promise<Response> {
      return new Response("second", { status: 200 });
    },
  };

  process.env.APP_ENV = "development";

  expect(() =>
    registerProductionInternalServiceTransport(first),
  ).toThrow(InternalServiceTransportConfigurationError);

  process.env.APP_ENV = "production";

  expect(() =>
    registerProductionInternalServiceTransport(first),
  ).not.toThrow();

  expect(getInternalServiceTransport()).toBe(first);

  // Idempotent registration of the exact same adapter is allowed.
  expect(() =>
    registerProductionInternalServiceTransport(first),
  ).not.toThrow();

  // Replacing an already registered adapter is rejected.
  expect(() =>
    registerProductionInternalServiceTransport(second),
  ).toThrow(InternalServiceTransportConfigurationError);

  expect(getInternalServiceTransport()).toBe(first);
});

test("getInternalServiceTransport resolves development and rejects unconfigured production", () => {
  process.env.APP_ENV = "development";
  process.env.OIDC_INTERNAL_SERVICE_SECRET = validSecret;

  const developmentTransport = getInternalServiceTransport();

  expect(developmentTransport).toBeDefined();

  process.env.APP_ENV = "production";

  expect(() =>
    getInternalServiceTransport(),
  ).toThrow(InternalServiceTransportConfigurationError);
});
