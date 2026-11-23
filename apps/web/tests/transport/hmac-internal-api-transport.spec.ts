import { createHash, createHmac } from "node:crypto";
import { test, expect } from "@playwright/test";

import {
  createHmacInternalServiceTransport,
  buildHmacCanonicalMessage,
  loadHmacInternalTransportConfiguration,
  HmacInternalTransportConfigurationError,
  type HmacInternalTransportConfiguration,
} from "../../src/lib/server/hmac-internal-api-transport";

const secret = Buffer.from("0123456789abcdef0123456789abcdef", "utf8");
const encodedSecret = secret.toString("base64url");
const serviceId = "web-bff";
const activeKeyId = "key-active-2026";

function config(
  overrides: Partial<HmacInternalTransportConfiguration> = {},
): HmacInternalTransportConfiguration {
  return {
    serviceId,
    activeKeyId,
    allowedOrigin: "https://api.example.test",
    keys: new Map([[activeKeyId, secret]]),
    ...overrides,
  };
}

function env(
  overrides: Record<string, string | undefined> = {},
): NodeJS.ProcessEnv {
  return {
    NODE_ENV: "test",
    APP_ENV: "production",
    ZAK_MARKET_DATA_API_BASE_URL: "https://api.example.test",
    OIDC_INTERNAL_SERVICE_ID: serviceId,
    OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID: activeKeyId,
    OIDC_INTERNAL_SERVICE_HMAC_KEYS: JSON.stringify({
      [activeKeyId]: encodedSecret,
    }),
    ...overrides,
  };
}

test.describe("HMAC internal API transport", () => {
  test("signs exact method, encoded target and body and sends all headers", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let capturedUrl: URL | undefined;
    let capturedInit: RequestInit | undefined;

    globalThis.fetch = (async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      capturedUrl = input instanceof URL ? input : new URL(String(input));
      capturedInit = init;
      return new Response("ok", { status: 200 });
    }) as typeof fetch;

    try {
      const url = new URL(
        "https://api.example.test/v1/items/%2Fpart?term=a%20b&tag=one&tag=two",
      );
      const body = '{"amount":12,"symbol":"BTCUSDT"}';

      const response = await transport.fetch(url, {
        method: "post",
        headers: { "x-correlation-id": "corr-123" },
        body,
      });

      expect(response.status).toBe(200);
      expect(capturedUrl?.href).toBe(url.href);
      expect(capturedInit?.method).toBe("POST");
      expect(capturedInit?.redirect).toBe("error");

      const headers = new Headers(capturedInit?.headers);
      expect(headers.get("x-correlation-id")).toBe("corr-123");
      expect(headers.get("X-Zak-Internal-Service-Id")).toBe(serviceId);
      expect(headers.get("X-Zak-Internal-Key-Id")).toBe(activeKeyId);

      const timestamp = headers.get("X-Zak-Internal-Timestamp");
      const nonce = headers.get("X-Zak-Internal-Nonce");
      const signature = headers.get("X-Zak-Internal-Signature");

      expect(timestamp).toMatch(/^\d{10}$/);
      expect(nonce).toMatch(/^[A-Za-z0-9_-]{43}$/);
      expect(signature).toMatch(/^[a-f0-9]{64}$/);

      const transmitted = Buffer.from(capturedInit?.body as Uint8Array);
      expect(transmitted.equals(Buffer.from(body, "utf8"))).toBe(true);

      const digest = createHash("sha256").update(transmitted).digest("hex");
      const canonical = [
        "ZAK-INTERNAL-REQUEST-V1",
        serviceId,
        activeKeyId,
        timestamp as string,
        nonce as string,
        "POST",
        url.pathname + url.search,
        digest,
      ].join("\n");

      const expected = createHmac("sha256", secret)
        .update(Buffer.from(canonical, "ascii"))
        .digest("hex");

      expect(signature).toBe(expected);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("uses active key from a multi-key rotation ring", async () => {
    const previous = Buffer.alloc(32, 7);
    const active = Buffer.alloc(32, 9);

    const transport = createHmacInternalServiceTransport(config({
      keys: new Map([
        ["key-previous", previous],
        [activeKeyId, active],
      ]),
    }));

    const originalFetch = globalThis.fetch;
    let headers: Headers | undefined;

    globalThis.fetch = (async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      headers = new Headers(init?.headers);
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      await transport.fetch(new URL("https://api.example.test/health"), {});
      expect(headers?.get("X-Zak-Internal-Key-Id")).toBe(activeKeyId);
      expect(headers?.get("X-Zak-Internal-Signature"))
        .toMatch(/^[a-f0-9]{64}$/);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("loads valid canonical Base64URL configuration", () => {
    const result = loadHmacInternalTransportConfiguration(env());

    expect(result.serviceId).toBe(serviceId);
    expect(result.activeKeyId).toBe(activeKeyId);
    expect(result.allowedOrigin).toBe("https://api.example.test");
    expect(result.keys.get(activeKeyId)?.equals(secret)).toBe(true);
  });

  test("rejects malformed key configuration", () => {
    const invalid = [
      env({ OIDC_INTERNAL_SERVICE_ID: undefined }),
      env({ ZAK_MARKET_DATA_API_BASE_URL: undefined }),
      env({ ZAK_MARKET_DATA_API_BASE_URL: "http://api.example.test" }),
      env({ ZAK_MARKET_DATA_API_BASE_URL: "https://user:pass@api.example.test" }),
      env({ ZAK_MARKET_DATA_API_BASE_URL: "https://api.example.test/path?query=1" }),
      env({ OIDC_INTERNAL_SERVICE_ID: "bad service id" }),
      env({ OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID: "missing-key" }),
      env({ OIDC_INTERNAL_SERVICE_HMAC_KEYS: "not-json" }),
      env({ OIDC_INTERNAL_SERVICE_HMAC_KEYS: "[]" }),
      env({ OIDC_INTERNAL_SERVICE_HMAC_KEYS: "{}" }),
      env({
        OIDC_INTERNAL_SERVICE_HMAC_KEYS: JSON.stringify({
          [activeKeyId]: "too-short",
        }),
      }),
      env({
        OIDC_INTERNAL_SERVICE_HMAC_KEYS: JSON.stringify({
          [activeKeyId]: `${encodedSecret}=`,
        }),
      }),
      env({
        OIDC_INTERNAL_SERVICE_HMAC_KEYS: JSON.stringify({
          [activeKeyId]: "A".repeat(42) + "+",
        }),
      }),
    ];

    for (const item of invalid) {
      expect(() => loadHmacInternalTransportConfiguration(item))
        .toThrow(HmacInternalTransportConfigurationError);
    }
  });

  test("rejects invalid direct factory configuration", () => {
    expect(() => createHmacInternalServiceTransport(config({
      serviceId: "invalid service",
    }))).toThrow(HmacInternalTransportConfigurationError);

    for (const allowedOrigin of [
      "http://api.example.test",
      "https://api.example.test/path",
      "https://user:pass@api.example.test",
      "https://api.example.test?query=1",
    ]) {
      expect(() => createHmacInternalServiceTransport(config({
        allowedOrigin,
      }))).toThrow(HmacInternalTransportConfigurationError);
    }

    expect(() => createHmacInternalServiceTransport(config({
      activeKeyId: "not-present",
    }))).toThrow(HmacInternalTransportConfigurationError);

    expect(() => createHmacInternalServiceTransport(config({
      keys: new Map([[activeKeyId, Buffer.alloc(8)]]),
    }))).toThrow(HmacInternalTransportConfigurationError);
  });

  test("hashes and transmits URLSearchParams bytes consistently", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let captured: RequestInit | undefined;

    globalThis.fetch = (async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      captured = init;
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      const body = new URLSearchParams([
        ["symbol", "BTCUSDT"],
        ["limit", "10"],
      ]);

      await transport.fetch(new URL("https://api.example.test/candles"), {
        method: "POST",
        body,
      });

      const headers = new Headers(captured?.headers);
      const transmitted = Buffer.from(captured?.body as Uint8Array);

      expect(transmitted.toString("utf8")).toBe(body.toString());
      expect(headers.get("content-type"))
        .toBe("application/x-www-form-urlencoded;charset=UTF-8");
      expect(headers.get("X-Zak-Internal-Signature"))
        .toMatch(/^[a-f0-9]{64}$/);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("preserves Uint8Array body bytes", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let captured: RequestInit | undefined;
    const bytes = new Uint8Array([0, 1, 2, 127, 128, 255]);

    globalThis.fetch = (async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      captured = init;
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      await transport.fetch(new URL("https://api.example.test/binary"), {
        method: "PUT",
        body: bytes,
      });

      expect(
        Buffer.from(captured?.body as Uint8Array).equals(Buffer.from(bytes)),
      ).toBe(true);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("represents empty body consistently as zero bytes", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let captured: RequestInit | undefined;

    globalThis.fetch = (async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      captured = init;
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      await transport.fetch(new URL("https://api.example.test/empty"), {
        method: "POST",
        body: "",
      });

      expect(captured?.body).toBeNull();

      expect(createHash("sha256").update(Buffer.alloc(0)).digest("hex"))
        .toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("rejects non-HTTPS URLs, URL credentials and fragments", async () => {
    const transport = createHmacInternalServiceTransport(config());

    const invalid = [
      new URL("http://api.example.test/internal"),
      new URL("https://user:pass@api.example.test/internal"),
      new URL("https://api.example.test/internal#fragment"),
    ];

    for (const url of invalid) {
      await expect(transport.fetch(url, {}))
        .rejects.toThrow(HmacInternalTransportConfigurationError);
    }
  });

  test("rejects HTTPS requests to unconfigured hosts and ports", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let calls = 0;

    globalThis.fetch = (async (): Promise<Response> => {
      calls += 1;
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      const invalidDestinations = [
        new URL("https://attacker.example/internal"),
        new URL("https://api.example.test:444/internal"),
        new URL("https://api.example.test.evil/internal"),
        new URL("http://api.example.test/internal"),
      ];

      for (const destination of invalidDestinations) {
        await expect(transport.fetch(destination, {}))
          .rejects.toThrow(HmacInternalTransportConfigurationError);
      }

      expect(calls).toBe(0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });

  test("rejects invalid methods and unsupported bodies before fetch", async () => {
    const transport = createHmacInternalServiceTransport(config());
    const originalFetch = globalThis.fetch;
    let calls = 0;

    globalThis.fetch = (async (): Promise<Response> => {
      calls += 1;
      return new Response(null, { status: 204 });
    }) as typeof fetch;

    try {
      await expect(transport.fetch(
        new URL("https://api.example.test/upload"),
        { method: "BAD METHOD" },
      )).rejects.toThrow(HmacInternalTransportConfigurationError);

      await expect(transport.fetch(
        new URL("https://api.example.test/upload"),
        { method: "POST", body: new Blob(["unsupported"]) },
      )).rejects.toThrow(HmacInternalTransportConfigurationError);

      expect(calls).toBe(0);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});




test("matches the shared Python HMAC canonical vector exactly", () => {
  const secret = Buffer.from(Array.from({ length: 32 }, (_, index) => index));
  const body = Buffer.from('{"amount":12,"symbol":"BTCUSDT"}', "utf8");
  const bodyDigest = createHash("sha256").update(body).digest("hex");

  const target =
    "/v1/items/%2Fpart?term=a%20b&tag=one&tag=two";

  const url = new URL(
    "https://api.example.test/v1/items/%2Fpart?term=a%20b&tag=one&tag=two",
  );

  expect(url.pathname + url.search).toBe(target);
  expect(bodyDigest).toBe(
    "167a94d35a6386ab481fc16e4d259c5ce35ff9431efb742d8c78c14029cfd719",
  );

  const canonical = buildHmacCanonicalMessage({
    serviceId: "web-bff",
    keyId: "key-2026-01",
    timestamp: "1730000000",
    nonce: "A".repeat(43),
    method: "POST",
    target,
    bodyDigest,
  });

  const signature = createHmac("sha256", secret)
    .update(canonical)
    .digest("hex");

  expect(signature).toBe(
    "d155e4203b4e8ed1f193a57e2702248e812a5f6c50b3f7536f32161f568af775",
  );
});


test("loads multiple keys from rotation environment configuration", () => {
  const previous = Buffer.alloc(32, 7);
  const active = Buffer.alloc(32, 9);

  const result = loadHmacInternalTransportConfiguration(
    env({
      OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID: "key-active-2026",
      OIDC_INTERNAL_SERVICE_HMAC_KEYS: JSON.stringify({
        "key-previous-2026": previous.toString("base64url"),
        "key-active-2026": active.toString("base64url"),
      }),
    }),
  );

  expect(result.activeKeyId).toBe("key-active-2026");
  expect(result.keys.get("key-previous-2026")?.equals(previous)).toBe(true);
  expect(result.keys.get("key-active-2026")?.equals(active)).toBe(true);
});
