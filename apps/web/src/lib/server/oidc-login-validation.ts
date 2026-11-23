export type OIDCTransactionResponse = {
  authorization_url: string;
  state: string;
  browser_binding_cookie: {
    name: string;
    value: string;
    max_age_seconds: number;
  };
  expires_at: string;
};

export type OIDCValidationConfig = {
  auth0Domain: string;
  clientId: string;
  callbackUrl: string;
};

export function validateOidcTransactionResponse(
  input: unknown,
  config: OIDCValidationConfig,
): { authorizationUrl: URL; cookie: OIDCTransactionResponse["browser_binding_cookie"] } {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("Invalid OIDC transaction response.");
  }

  const data = input as Record<string, unknown>;
  const expectedKeys = [
    "authorization_url",
    "state",
    "browser_binding_cookie",
    "expires_at",
  ];

  if (
    Object.keys(data).length !== expectedKeys.length ||
    !expectedKeys.every((key) => Object.hasOwn(data, key))
  ) {
    throw new Error("Invalid OIDC transaction response.");
  }

  if (
    typeof data.state !== "string" ||
    !/^[A-Za-z0-9_-]{43,128}$/.test(data.state) ||
    typeof data.authorization_url !== "string" ||
    data.authorization_url.length < 1 ||
    data.authorization_url.length > 4096 ||
    typeof data.expires_at !== "string"
  ) {
    throw new Error("Invalid OIDC transaction response.");
  }

  const expiry = Date.parse(data.expires_at);
  if (!Number.isFinite(expiry) || expiry <= Date.now()) {
    throw new Error("Expired OIDC transaction.");
  }

  const rawCookie = data.browser_binding_cookie;
  if (!rawCookie || typeof rawCookie !== "object" || Array.isArray(rawCookie)) {
    throw new Error("Invalid OIDC browser-binding cookie.");
  }

  const cookie = rawCookie as Record<string, unknown>;
  const cookieKeys = ["name", "value", "max_age_seconds"];

  if (
    Object.keys(cookie).length !== cookieKeys.length ||
    !cookieKeys.every((key) => Object.hasOwn(cookie, key)) ||
    typeof cookie.name !== "string" ||
    !/^__Host-zak_oidc_[0-9a-f]{24}$/.test(cookie.name) ||
    typeof cookie.value !== "string" ||
    !/^[A-Za-z0-9_-]{43}$/.test(cookie.value) ||
    cookie.max_age_seconds !== 300
  ) {
    throw new Error("Invalid OIDC browser-binding cookie.");
  }

  let authorizationUrl: URL;

  try {
    authorizationUrl = new URL(data.authorization_url);
  } catch {
    throw new Error("Invalid OIDC authorization URL.");
  }

  const domain = config.auth0Domain.toLowerCase();

  if (
    authorizationUrl.protocol !== "https:" ||
    authorizationUrl.hostname.toLowerCase() !== domain ||
    (authorizationUrl.port !== "" && authorizationUrl.port !== "443") ||
    authorizationUrl.username !== "" ||
    authorizationUrl.password !== "" ||
    authorizationUrl.hash !== "" ||
    authorizationUrl.pathname === "" ||
    authorizationUrl.pathname === "/"
  ) {
    throw new Error("Untrusted OIDC authorization destination.");
  }

  const requiredParameters = [
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "nonce",
    "code_challenge",
    "code_challenge_method",
  ];

  const params = authorizationUrl.searchParams;
  const actualKeys = [...params.keys()];

  if (
    actualKeys.length !== requiredParameters.length ||
    !requiredParameters.every(
      (key) => params.getAll(key).length === 1,
    ) ||
    !requiredParameters.every((key) => actualKeys.includes(key))
  ) {
    throw new Error("Invalid OIDC authorization parameters.");
  }

  if (
    params.get("response_type") !== "code" ||
    params.get("client_id") !== config.clientId ||
    params.get("redirect_uri") !== config.callbackUrl ||
    params.get("scope") !== "openid email profile" ||
    params.get("state") !== data.state ||
    params.get("code_challenge_method") !== "S256" ||
    !/^[A-Za-z0-9_-]{43,128}$/.test(params.get("nonce") ?? "") ||
    !/^[A-Za-z0-9_-]{43}$/.test(params.get("code_challenge") ?? "")
  ) {
    throw new Error("OIDC authorization parameters failed validation.");
  }

  return {
    authorizationUrl,
    cookie: {
      name: cookie.name as string,
      value: cookie.value as string,
      max_age_seconds: 300,
    },
  };
}
