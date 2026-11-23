import "server-only";

import { createHash, createHmac, randomBytes } from "node:crypto";

import type { InternalServiceTransport } from "./internal-api-transport";
import { parseInternalApiBaseUrl } from "./internal-api-validation";

const PROTOCOL_VERSION = "ZAK-INTERNAL-REQUEST-V1";

const HEADER_SERVICE_ID = "X-Zak-Internal-Service-Id";
const HEADER_KEY_ID = "X-Zak-Internal-Key-Id";
const HEADER_TIMESTAMP = "X-Zak-Internal-Timestamp";
const HEADER_NONCE = "X-Zak-Internal-Nonce";
const HEADER_SIGNATURE = "X-Zak-Internal-Signature";

const SERVICE_ID_PATTERN = /^[A-Za-z0-9._-]{1,128}$/;
const KEY_ID_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

export class HmacInternalTransportConfigurationError extends Error {
  constructor() {
    super("Production HMAC transport configuration is invalid.");
    this.name = "HmacInternalTransportConfigurationError";
  }
}

export interface HmacInternalTransportConfiguration {
  serviceId: string;
  activeKeyId: string;
  allowedOrigin: string;
  keys: ReadonlyMap<string, Buffer>;
}

function decodeBase64UrlSecret(value: unknown): Buffer {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    !/^[A-Za-z0-9_-]+$/.test(value)
  ) {
    throw new HmacInternalTransportConfigurationError();
  }

  const decoded = Buffer.from(value, "base64url");

  if (
    decoded.length < 32 ||
    decoded.toString("base64url") !== value
  ) {
    throw new HmacInternalTransportConfigurationError();
  }

  return decoded;
}

export function loadHmacInternalTransportConfiguration(
  environment: NodeJS.ProcessEnv = process.env,
): HmacInternalTransportConfiguration {
  const baseUrl = parseInternalApiBaseUrl(
    environment.ZAK_MARKET_DATA_API_BASE_URL,
    environment.APP_ENV,
  );

  if (!baseUrl || baseUrl.protocol !== "https:") {
    throw new HmacInternalTransportConfigurationError();
  }

  const allowedOrigin = baseUrl.origin;
  const serviceId = environment.OIDC_INTERNAL_SERVICE_ID;
  const activeKeyId =
    environment.OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID;
  const rawKeyRing =
    environment.OIDC_INTERNAL_SERVICE_HMAC_KEYS;

  if (
    typeof serviceId !== "string" ||
    !SERVICE_ID_PATTERN.test(serviceId) ||
    typeof activeKeyId !== "string" ||
    !KEY_ID_PATTERN.test(activeKeyId) ||
    typeof rawKeyRing !== "string" ||
    rawKeyRing.length === 0
  ) {
    throw new HmacInternalTransportConfigurationError();
  }

  let parsed: unknown;

  try {
    parsed = JSON.parse(rawKeyRing);
  } catch {
    throw new HmacInternalTransportConfigurationError();
  }

  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed)
  ) {
    throw new HmacInternalTransportConfigurationError();
  }

  const entries = Object.entries(parsed as Record<string, unknown>);

  if (entries.length === 0) {
    throw new HmacInternalTransportConfigurationError();
  }

  const keys = new Map<string, Buffer>();

  for (const [keyId, encodedSecret] of entries) {
    if (!KEY_ID_PATTERN.test(keyId)) {
      throw new HmacInternalTransportConfigurationError();
    }

    keys.set(keyId, decodeBase64UrlSecret(encodedSecret));
  }

  if (!keys.has(activeKeyId)) {
    throw new HmacInternalTransportConfigurationError();
  }

  return Object.freeze({
    serviceId,
    activeKeyId,
    allowedOrigin,
    keys,
  });
}

function getExactBodyBytes(
  body: BodyInit | null | undefined,
): Buffer {
  if (body === undefined || body === null) {
    return Buffer.alloc(0);
  }

  if (typeof body === "string") {
    return Buffer.from(body, "utf8");
  }

  if (body instanceof URLSearchParams) {
    return Buffer.from(body.toString(), "utf8");
  }

  if (body instanceof ArrayBuffer) {
    return Buffer.from(new Uint8Array(body));
  }

  if (body instanceof Uint8Array) {
    return Buffer.from(body);
  }

  throw new HmacInternalTransportConfigurationError();
}

export function buildHmacCanonicalMessage(input: {
  serviceId: string;
  keyId: string;
  timestamp: string;
  nonce: string;
  method: string;
  target: string;
  bodyDigest: string;
}): Buffer {
  const fields = [
    PROTOCOL_VERSION,
    input.serviceId,
    input.keyId,
    input.timestamp,
    input.nonce,
    input.method,
    input.target,
    input.bodyDigest,
  ];

  if (fields.some((field) => /[\r\n]/.test(field))) {
    throw new HmacInternalTransportConfigurationError();
  }

  return Buffer.from(fields.join("\n"), "ascii");
}

export function createHmacInternalServiceTransport(
  configuration: HmacInternalTransportConfiguration,
): InternalServiceTransport {
  let allowedOrigin: string;

  try {
    const parsedOrigin = new URL(configuration.allowedOrigin);

    if (
      parsedOrigin.protocol !== "https:" ||
      parsedOrigin.origin !== configuration.allowedOrigin ||
      parsedOrigin.pathname !== "/" ||
      parsedOrigin.search !== "" ||
      parsedOrigin.hash !== "" ||
      parsedOrigin.username !== "" ||
      parsedOrigin.password !== ""
    ) {
      throw new HmacInternalTransportConfigurationError();
    }

    allowedOrigin = parsedOrigin.origin;
  } catch {
    throw new HmacInternalTransportConfigurationError();
  }

  if (
    !SERVICE_ID_PATTERN.test(configuration.serviceId) ||
    !KEY_ID_PATTERN.test(configuration.activeKeyId) ||
    !(configuration.keys instanceof Map) ||
    !configuration.keys.has(configuration.activeKeyId)
  ) {
    throw new HmacInternalTransportConfigurationError();
  }

  const secret = configuration.keys.get(configuration.activeKeyId);

  if (!Buffer.isBuffer(secret) || secret.length < 32) {
    throw new HmacInternalTransportConfigurationError();
  }

  const serviceId = configuration.serviceId;
  const keyId = configuration.activeKeyId;

  return Object.freeze({
    async fetch(url: URL, init: RequestInit): Promise<Response> {
      if (
        !(url instanceof URL) ||
        url.protocol !== "https:" ||
        url.origin !== allowedOrigin ||
        url.username !== "" ||
        url.password !== "" ||
        url.hash !== ""
      ) {
        throw new HmacInternalTransportConfigurationError();
      }

      const method = (init.method ?? "GET").toUpperCase();

      if (!/^[A-Z]+$/.test(method)) {
        throw new HmacInternalTransportConfigurationError();
      }

      const target = url.pathname + url.search;

      if (/[\r\n]/.test(target)) {
        throw new HmacInternalTransportConfigurationError();
      }

      const bodyBytes = getExactBodyBytes(init.body);
      const bodyDigest = createHash("sha256")
        .update(bodyBytes)
        .digest("hex");

      const timestamp = Math.floor(Date.now() / 1000).toString();
      const nonce = randomBytes(32).toString("base64url");

      const canonicalMessage = buildHmacCanonicalMessage({
        serviceId,
        keyId,
        timestamp,
        nonce,
        method,
        target,
        bodyDigest,
      });

      const signature = createHmac("sha256", secret)
        .update(canonicalMessage)
        .digest("hex");

      const headers = new Headers(init.headers);

      headers.set(HEADER_SERVICE_ID, serviceId);
      headers.set(HEADER_KEY_ID, keyId);
      headers.set(HEADER_TIMESTAMP, timestamp);
      headers.set(HEADER_NONCE, nonce);
      headers.set(HEADER_SIGNATURE, signature);

      if (init.body instanceof URLSearchParams && !headers.has("content-type")) {
        headers.set(
          "content-type",
          "application/x-www-form-urlencoded;charset=UTF-8",
        );
      }

      const outgoingInit: RequestInit = {
        ...init,
        method,
        headers,
        redirect: "error",
        body: bodyBytes.length > 0 ? new Uint8Array(bodyBytes) : null,
      };

      return fetch(url, outgoingInit);
    },
  });
}

/**
 * Deployment factory entrypoint.
 * Configure:
 * OIDC_INTERNAL_SERVICE_ID=web-bff
 * OIDC_INTERNAL_SERVICE_HMAC_ACTIVE_KEY_ID=key-2026-01
 * OIDC_INTERNAL_SERVICE_HMAC_KEYS={"key-2026-01":"BASE64URL_SECRET"}
 */
export function buildProductionInternalServiceTransport():
  InternalServiceTransport {
  const configuration = loadHmacInternalTransportConfiguration();
  return createHmacInternalServiceTransport(configuration);
}