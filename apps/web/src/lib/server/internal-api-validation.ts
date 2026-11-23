export function parseInternalApiBaseUrl(
  configured: string | undefined,
  appEnv: string | undefined,
): URL | null {
  if (
    typeof configured !== "string" ||
    configured.length === 0 ||
    configured.trim() !== configured
  ) {
    return null;
  }

  try {
    const base = new URL(configured);
    const environment = appEnv?.trim().toLowerCase();
    const localHost = ["localhost", "127.0.0.1", "[::1]"].includes(
      base.hostname.toLowerCase(),
    );

    const secureTransport =
      base.protocol === "https:" ||
      (
        base.protocol === "http:" &&
        environment === "development" &&
        localHost
      );

    if (
      !secureTransport ||
      base.hostname.length === 0 ||
      base.username !== "" ||
      base.password !== "" ||
      base.search !== "" ||
      base.hash !== ""
    ) {
      return null;
    }

    return base;
  } catch {
    return null;
  }
}

export function parseAppPublicOrigin(
  configured: string | undefined,
  appEnv: string | undefined,
): URL | null {
  if (
    typeof configured !== "string" ||
    configured.length === 0 ||
    configured.trim() !== configured
  ) {
    return null;
  }

  try {
    const parsed = new URL(configured);
    const environment = appEnv?.trim().toLowerCase();
    const localHost = [
      "localhost",
      "127.0.0.1",
      "[::1]",
    ].includes(parsed.hostname.toLowerCase());

    const secureTransport =
      parsed.protocol === "https:" ||
      (
        parsed.protocol === "http:" &&
        environment === "development" &&
        localHost
      );

    if (
      !secureTransport ||
      parsed.hostname.length === 0 ||
      parsed.username !== "" ||
      parsed.password !== "" ||
      parsed.pathname !== "/" ||
      parsed.search !== "" ||
      parsed.hash !== ""
    ) {
      return null;
    }

    return parsed;
  } catch {
    return null;
  }
}
const STATIC_INTERNAL_ENDPOINTS = new Map<string, ReadonlySet<string>>([
  ["/internal/v1/auth/oidc/transactions", new Set(["POST"])],
  ["/internal/v1/auth/oidc/callback", new Set(["POST"])],
  ["/internal/v1/auth/session/revoke", new Set(["POST"])],
  ["/internal/v1/auth/session/validate", new Set(["POST"])],
  ["/api/v1/market-data/health", new Set(["GET"])],
  ["/api/v1/paper-trading/bootstrap", new Set(["GET"])],
]);

export function isAllowedInternalApiRequest(
  path: string,
  method: string,
): boolean {
  if (
    typeof path !== "string" ||
    typeof method !== "string" ||
    !/^[A-Z]+$/i.test(method) ||
    !path.startsWith("/") ||
    path.startsWith("//") ||
    path.includes("\\") ||
    /[\r\n?#]/.test(path) ||
    /%(?:2f|5c|2e)/i.test(path)
  ) {
    return false;
  }

  const normalizedMethod = method.toUpperCase();
  const staticMethods = STATIC_INTERNAL_ENDPOINTS.get(path);

  if (staticMethods) {
    return staticMethods.has(normalizedMethod);
  }

  const paperSpotTradePath =
    /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/paper-trades\/spot$/;

  if (paperSpotTradePath.test(path)) {
    return normalizedMethod === "POST";
  }

  const paperFuturesTradePath =
    /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/paper-trades\/futures$/;

  if (paperFuturesTradePath.test(path)) {
    return normalizedMethod === "POST";
  }
  const paperManualTopUpPath =
    /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/manual-top-up$/;

  if (paperManualTopUpPath.test(path)) {
    return normalizedMethod === "POST";
  }

  const paperPortfolioResetPath =
    /^\/api\/v1\/accounts\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/portfolios\/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\/reset$/;

  if (paperPortfolioResetPath.test(path)) {
    return normalizedMethod === "POST";
  }

  const latestMarketDataPath =
    /^\/api\/v1\/market-data\/latest\/(?:spot|futures_mark_price)\/[A-Z0-9]{2,30}$/;

  return (
    latestMarketDataPath.test(path) &&
    normalizedMethod === "GET"
  );
}


/**
 * Pure URL builder shared by the server-only API client and tests.
 * Returns null when the path, method, or configured base is invalid.
 */
export function buildInternalApiUrl(
  path: string,
  method: string,
  configured: string | undefined,
  appEnv: string | undefined,
): URL | null {
  if (
    typeof path !== "string" ||
    typeof method !== "string" ||
    !isAllowedInternalApiRequest(path, method)
  ) {
    return null;
  }

  const base = parseInternalApiBaseUrl(configured, appEnv);

  if (!base) {
    return null;
  }

  try {
    const basePath = base.pathname.replace(/\/+$/, "");
    const endpointPath = `${basePath}/${path.slice(1)}`;

    return new URL(endpointPath, base.origin);
  } catch {
    return null;
  }
}
