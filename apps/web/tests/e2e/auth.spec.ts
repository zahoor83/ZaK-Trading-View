import { randomBytes, randomUUID, createHash } from "node:crypto";
import {
  test,
  expect,
  type Page,
} from "@playwright/test";

import {
  buildInternalApiUrl,
  isAllowedInternalApiRequest,
  parseAppPublicOrigin,
  parseInternalApiBaseUrl,
} from "../../src/lib/server/internal-api-validation";
import {
  LEGACY_SESSION_COOKIE_NAME,
  readSingleSessionToken,
  SESSION_COOKIE_NAME,
} from "../../src/lib/server/session-cookie";

const APP_ORIGIN = "http://localhost:3000";
const MOCK_ORIGIN = "http://127.0.0.1:8000";

function browserCookieName(state: string): string {
  const hash = createHash("sha256")
    .update(Buffer.from("state", "ascii"))
    .update(Buffer.from([0]))
    .update(Buffer.from(state, "ascii"))
    .digest("hex");

  return `__Host-zak_oidc_${hash.slice(0, 24)}`;
}

async function beginLogin(page: Page): Promise<{
  state: string;
  bindingCookieName: string;
}> {
  let authorizationUrl: URL | undefined;

  page.on("requestfailed", (request) => {
    const failedUrl = new URL(request.url());
    console.log("[E2E REQUEST FAILED]", request.method(), failedUrl.origin, failedUrl.pathname, request.failure()?.errorText);
  });

  page.on("response", (response) => {
    const url = new URL(response.url());
    if (
      url.origin === APP_ORIGIN &&
      url.pathname === "/api/auth/oidc/login" &&
      response.request().method() === "POST"
    ) {
      const location = response.headers()["location"] ?? "";
      console.log("[E2E LOGIN RESPONSE]", response.status(), location ? "(redirect present)" : "(no Location header)");

      if (response.status() === 303 && location) {
        authorizationUrl = new URL(location);
        console.log("[E2E AUTHORIZATION URL CAPTURED FROM 303]");
      }
    }
  });

  await page.route("**/*", async (route) => {
    const requestUrl = new URL(route.request().url());

    if (requestUrl.hostname === "example.auth0.com") {
      authorizationUrl = requestUrl;
      console.log("[E2E AUTH0 INTERCEPTED]", requestUrl.origin, requestUrl.pathname);

      await route.fulfill({
        status: 200,
        contentType: "text/html; charset=utf-8",
        body: "<!doctype html><html><body>Mock identity provider</body></html>",
      });
      return;
    }

    await route.continue();
  });

  await page.goto("/login", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("heading", { name: "Sign in" }),
  ).toBeVisible();

  const loginResponsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());
      return (
        url.origin === APP_ORIGIN &&
        url.pathname === "/api/auth/oidc/login" &&
        response.request().method() === "POST"
      );
    },
    { timeout: 20_000 },
  );

  await page
    .getByRole("button", {
      name: "Continue with secure sign-in",
    })
    .click();

  const loginResponse = await loginResponsePromise;
  const loginLocation = loginResponse.headers()["location"] ?? "";

  console.log("[E2E LOGIN STATUS]", loginResponse.status());
  console.log("[E2E LOGIN LOCATION]", loginLocation ? "(present)" : "(empty)");

  if (loginResponse.status() !== 303) {
    
    throw new Error(
      `OIDC login endpoint returned unexpected status ${loginResponse.status()}.`,
    );
  }

  await expect
    .poll(() => authorizationUrl?.toString() ?? "", {
      timeout: 10_000,
    })
    .not.toBe("");

  if (!authorizationUrl) {
    throw new Error("Auth0 authorization redirect was not observed.");
  }

  expect(authorizationUrl.origin).toBe("https://example.auth0.com");
  expect(authorizationUrl.searchParams.get("response_type")).toBe("code");
  expect(authorizationUrl.searchParams.get("client_id")).toBe(
    "zak-e2e-client",
  );
  expect(authorizationUrl.searchParams.get("redirect_uri")).toBe(
    `${APP_ORIGIN}/api/auth/oidc/callback`,
  );
  expect(authorizationUrl.searchParams.get("scope")).toBe(
    "openid email profile",
  );
  expect(authorizationUrl.searchParams.get("code_challenge_method")).toBe(
    "S256",
  );

  const state = authorizationUrl.searchParams.get("state");

  if (!state) {
    throw new Error("OIDC state is missing.");
  }

  const bindingCookieName = browserCookieName(state);
  const cookies = await page.context().cookies(APP_ORIGIN);
  const bindingCookie = cookies.find(
    (cookie) => cookie.name === bindingCookieName,
  );

  expect(bindingCookie).toBeDefined();
  expect(bindingCookie?.httpOnly).toBe(true);
  expect(bindingCookie?.secure).toBe(true);
  expect(bindingCookie?.sameSite).toBe("Lax");
  expect(bindingCookie?.path).toBe("/");

  return {
    state,
    bindingCookieName,
  };
}

async function establishBrowserSession(page: Page): Promise<string> {
  const { state } = await beginLogin(page);

  await page.goto(
    `/api/auth/oidc/callback?code=mock-authorization-code&state=${encodeURIComponent(state)}`,
    { waitUntil: "domcontentloaded" },
  );

  await expect(page).toHaveURL(`${APP_ORIGIN}/`);

  await expect(
    page.getByText("zak-e2e@example.test"),
  ).toBeVisible();

  const cookies = await page.context().cookies(APP_ORIGIN);

  const sessionCookie = cookies.find(
    (cookie) => cookie.name === SESSION_COOKIE_NAME,
  );

  if (!sessionCookie?.value) {
    throw new Error("Authenticated browser session cookie was not issued.");
  }

  return sessionCookie.value;
}

test("unauthenticated users are redirected to login", async ({ page }) => {
  await page.goto("/markets", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/login$/);
  await expect(
    page.getByRole("heading", { name: "Sign in" }),
  ).toBeVisible();
});

test("OIDC login, callback, protected session and logout lifecycle", async ({
  page,
}) => {
  const { state } = await beginLogin(page);

  const callbackResponsePromise = page.waitForResponse(
    (response) => {
      const url = new URL(response.url());

      return (
        url.origin === APP_ORIGIN &&
        url.pathname === "/api/auth/oidc/callback" &&
        response.request().method() === "GET"
      );
    },
  );

  const callbackStartedAt = Date.now();

  await page.goto(
    `/api/auth/oidc/callback?code=mock-authorization-code&state=${encodeURIComponent(state)}`,
  );

  const callbackResponse = await callbackResponsePromise;
  const callbackFinishedAt = Date.now();

  expect(callbackResponse.status()).toBeGreaterThanOrEqual(300);
  expect(callbackResponse.status()).toBeLessThan(400);

  await expect(page).toHaveURL(`${APP_ORIGIN}/`);

  await expect(
    page.getByText("zak-e2e@example.test"),
  ).toBeVisible();

  const cookiesAfterLogin = await page.context().cookies(APP_ORIGIN);
  const sessionCookie = cookiesAfterLogin.find(
    (cookie) => cookie.name === SESSION_COOKIE_NAME,
  );

  expect(sessionCookie).toBeDefined();
  expect(sessionCookie?.httpOnly).toBe(true);
  expect(sessionCookie?.secure).toBe(true);
  expect(sessionCookie?.sameSite).toBe("Lax");
  expect(sessionCookie?.path).toBe("/");
  expect(sessionCookie?.name).toBe(SESSION_COOKIE_NAME);

  // Browser cookie must reflect the actual callback Set-Cookie expiry.
  expect(sessionCookie).toBeDefined();

  // Inspect the actual callback Set-Cookie header.
  const callbackHeaders = await callbackResponse.headersArray();

  const callbackSetCookies = callbackHeaders
    .filter((header) => header.name.toLowerCase() === "set-cookie")
    .map((header) => header.value);

  const issuedSessionHeader = callbackSetCookies.find(
    (value) => value.startsWith(`${SESSION_COOKIE_NAME}=`),
  );

  expect(issuedSessionHeader).toBeDefined();

  const maxAgeMatch = issuedSessionHeader!.match(
    /;\s*Max-Age=(\d+)(?:;|$)/i,
  );

  const expiresMatch = issuedSessionHeader!.match(
    /;\s*Expires=([^;]+)(?:;|$)/i,
  );

  expect(maxAgeMatch).not.toBeNull();
  expect(expiresMatch).not.toBeNull();

  const issuedMaxAge = Number(maxAgeMatch![1]);
  const headerExpiryMs = Date.parse(expiresMatch![1]);

  expect(Number.isFinite(headerExpiryMs)).toBe(true);
  expect(headerExpiryMs).toBeGreaterThan(Date.now());

  expect(Number.isInteger(issuedMaxAge)).toBe(true);
  expect(issuedMaxAge).toBeGreaterThan(0);
  expect(issuedMaxAge).toBeLessThanOrEqual(12 * 60 * 60 + 30);

  // The browser's stored expiry must match the actual Set-Cookie Expires.
  // HTTP-date and browser cookie precision are checked with a 1.5s bound.
  expect(
    Math.abs(sessionCookie!.expires * 1000 - headerExpiryMs),
  ).toBeLessThanOrEqual(1500);

  // Max-Age is an integer duration; Expires is an absolute HTTP date.
  // Allow 2 seconds for HTTP-date second precision and response timing.
  const minimumMaxAge = Math.floor(
    (headerExpiryMs - callbackFinishedAt) / 1000,
  ) - 2;

  const maximumMaxAge = Math.ceil(
    (headerExpiryMs - callbackStartedAt) / 1000,
  ) + 2;

  expect(issuedMaxAge).toBeGreaterThanOrEqual(minimumMaxAge);
  expect(issuedMaxAge).toBeLessThanOrEqual(maximumMaxAge);
  expect(
    cookiesAfterLogin.some(
      (cookie) => cookie.name === LEGACY_SESSION_COOKIE_NAME,
    ),
  ).toBe(false);

  const bindingCookie = cookiesAfterLogin.find(
    (cookie) => cookie.name.startsWith("__Host-zak_oidc_"),
  );

  expect(bindingCookie).toBeUndefined();

  // Cross-origin logout must be rejected without destroying the session.
  const csrfResponse = await page.request.post(
    `${APP_ORIGIN}/api/auth/logout`,
    {
      headers: {
        origin: "https://attacker.example",
        "sec-fetch-site": "cross-site",
      },
    },
  );

  expect(csrfResponse.status()).toBe(403);

  const cookiesAfterCsrf = await page.context().cookies(APP_ORIGIN);

  expect(
    cookiesAfterCsrf.some((cookie) => cookie.name === SESSION_COOKIE_NAME),
  ).toBe(true);

  // Logout must reject duplicate and malformed raw session cookies
  // before contacting the backend revoke endpoint.
  const activeCookies = await page.context().cookies(APP_ORIGIN);
  const activeSessionCookie = activeCookies.find(
    (cookie) => cookie.name === SESSION_COOKIE_NAME,
  );

  expect(activeSessionCookie?.value).toBeTruthy();

  const invalidLogoutCookieCases = [
    {
      name: "duplicate session cookie",
      cookie: `${SESSION_COOKIE_NAME}=${activeSessionCookie!.value}; ${SESSION_COOKIE_NAME}=${activeSessionCookie!.value}`,
    },
    {
      name: "malformed short session token",
      cookie: `${SESSION_COOKIE_NAME}=short`,
    },
  ];

  for (const scenario of invalidLogoutCookieCases) {
    const invalidLogout = await page.request.post(
      `${APP_ORIGIN}/api/auth/logout`,
      {
        headers: {
          origin: APP_ORIGIN,
          "sec-fetch-site": "same-origin",
          cookie: scenario.cookie,
        },
      },
    );

    expect(
      invalidLogout.status(),
      `Logout must reject ${scenario.name}`,
    ).toBe(400);

    const cookiesAfterInvalidLogout =
      await page.context().cookies(APP_ORIGIN);

    expect(
      cookiesAfterInvalidLogout.some(
        (cookie) => cookie.name === SESSION_COOKIE_NAME,
      ),
      `Rejected ${scenario.name} must not clear the browser session cookie`,
    ).toBe(true);
  }
  // Simulate an unavailable backend revoke endpoint.
  const failRevokeResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/fail-revoke?enabled=true`,
  );

  expect(failRevokeResponse.ok()).toBe(true);

  const failedLogoutPromise = page.waitForResponse(
    (response) =>
      response.url().includes("/api/auth/logout") &&
      response.request().method() === "POST",
  );

  await page.getByRole("button", { name: "Sign out" }).click();

  const failedLogout = await failedLogoutPromise;
  expect(failedLogout.status()).toBe(503);

  const cookiesAfterFailedLogout = await page.context().cookies(APP_ORIGIN);

  expect(
    cookiesAfterFailedLogout.some(
      (cookie) => cookie.name === SESSION_COOKIE_NAME,
    ),
  ).toBe(true);

  // Restore the mock backend and retry logout.
  const restoreRevokeResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/fail-revoke?enabled=false`,
  );

  expect(restoreRevokeResponse.ok()).toBe(true);

  await page.goto("/");
  await expect(
    page.getByText("zak-e2e@example.test"),
  ).toBeVisible();

  const successfulLogoutPromise = page.waitForResponse(
    (response) =>
      response.url().includes("/api/auth/logout") &&
      response.request().method() === "POST",
  );

  await page.getByRole("button", { name: "Sign out" }).click();

  const successfulLogout = await successfulLogoutPromise;

  expect(successfulLogout.status()).toBe(303);

  const logoutHeaders = await successfulLogout.headersArray();

  const logoutSetCookies = logoutHeaders
    .filter((header) => header.name.toLowerCase() === "set-cookie")
    .map((header) => header.value);

  for (const cookieName of [
    SESSION_COOKIE_NAME,
    LEGACY_SESSION_COOKIE_NAME,
  ]) {
    const clearingHeader = logoutSetCookies.find(
      (value) => value.startsWith(`${cookieName}=`),
    );

    expect(
      clearingHeader,
      `Logout must clear cookie: ${cookieName}`,
    ).toBeDefined();

    expect(clearingHeader!.startsWith(`${cookieName}=;`)).toBe(true);
    expect(clearingHeader).toMatch(/;\s*Max-Age=0(?:;|$)/i);

    expect(clearingHeader).toMatch(
      /;\s*Expires=Thu, 01 Jan 1970 00:00:00 GMT(?:;|$)/i,
    );

    expect(clearingHeader).toMatch(/;\s*Path=\/(?:;|$)/i);
    expect(clearingHeader).toMatch(/;\s*HttpOnly(?:;|$)/i);
    expect(clearingHeader).toMatch(/;\s*Secure(?:;|$)/i);
    expect(clearingHeader).toMatch(/;\s*SameSite=Lax(?:;|$)/i);
  }

  await expect(page).toHaveURL(/\/login$/);

  const cookiesAfterLogout = await page.context().cookies(APP_ORIGIN);

  expect(
    cookiesAfterLogout.some((cookie) => cookie.name === SESSION_COOKIE_NAME),
  ).toBe(false);
});

test("cross-origin login initiation is rejected", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}/api/auth/oidc/login`,
    {
      headers: {
        origin: "https://attacker.example",
        "sec-fetch-site": "cross-site",
      },
    },
  );

  expect(response.status()).toBe(403);
});

test("login initiation rejects a missing Origin header", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}/api/auth/oidc/login`,
    {
      headers: {
        "sec-fetch-site": "same-origin",
      },
    },
  );

  expect(response.status()).toBe(403);
});

test("login initiation rejects invalid Fetch Metadata with an exact Origin", async ({
  page,
}) => {
  const response = await page.request.post(
    `${APP_ORIGIN}/api/auth/oidc/login`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "cross-site",
      },
    },
  );

  expect(response.status()).toBe(403);
});

test("login initiation rejects a null Origin", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}/api/auth/oidc/login`,
    {
      headers: {
        origin: "null",
        "sec-fetch-site": "same-origin",
      },
    },
  );

  expect(response.status()).toBe(403);
});

test("login initiation rejects same-site and none Fetch Metadata", async ({
  page,
}) => {
  for (const fetchSite of ["same-site", "none"]) {
    const response = await page.request.post(
      `${APP_ORIGIN}/api/auth/oidc/login`,
      {
        headers: {
          origin: APP_ORIGIN,
          "sec-fetch-site": fetchSite,
        },
      },
    );

    expect(
      response.status(),
      `Login must reject Sec-Fetch-Site: ${fetchSite}`,
    ).toBe(403);
  }
});

test("logout rejects missing and null Origin headers", async ({ page }) => {
  const missingOriginResponse = await page.request.post(
    `${APP_ORIGIN}/api/auth/logout`,
    {
      headers: {
        "sec-fetch-site": "same-origin",
      },
    },
  );

  expect(missingOriginResponse.status()).toBe(403);

  const nullOriginResponse = await page.request.post(
    `${APP_ORIGIN}/api/auth/logout`,
    {
      headers: {
        origin: "null",
        "sec-fetch-site": "same-origin",
      },
    },
  );

  expect(nullOriginResponse.status()).toBe(403);
});

test("logout rejects same-site and none Fetch Metadata", async ({ page }) => {
  for (const fetchSite of ["same-site", "none"]) {
    const response = await page.request.post(
      `${APP_ORIGIN}/api/auth/logout`,
      {
        headers: {
          origin: APP_ORIGIN,
          "sec-fetch-site": fetchSite,
        },
      },
    );

    expect(
      response.status(),
      `Logout must reject Sec-Fetch-Site: ${fetchSite}`,
    ).toBe(403);
  }
});

test("login initiation rejects malformed and untrusted transaction responses", async ({
  page,
}) => {
  const modes = [
    "malformed",
    "expired",
    "wrong-host",
    "wrong-client",
    "wrong-callback",
    "duplicate-parameter",
  ];

  try {
    for (const mode of modes) {
      const controlResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/oidc-transaction-response?mode=${mode}`,
      );

      expect(
        controlResponse.status(),
        `Mock server must enable mode: ${mode}`,
      ).toBe(200);

      const response = await page.request.post(
        `${APP_ORIGIN}/api/auth/oidc/login`,
        {
          headers: {
            origin: APP_ORIGIN,
            "sec-fetch-site": "same-origin",
          },
        },
      );

      expect(
        response.status(),
        `Login must reject transaction mode: ${mode}`,
      ).toBe(503);

      const responseHeaders = response.headers();

      expect(
        responseHeaders.location,
        `Unsafe redirect must not be issued for mode: ${mode}`,
      ).toBeUndefined();

      expect(
        responseHeaders["set-cookie"],
        `Browser-binding cookie must not be issued for mode: ${mode}`,
      ).toBeUndefined();
    }
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/oidc-transaction-response?mode=valid`,
    );

    expect(resetResponse.status()).toBe(200);
  }
});
test("callback without the matching browser-binding cookie is rejected", async ({
  page,
}) => {
  const state = randomBytes(32).toString("base64url");

  const response = await page.request.get(
    `${APP_ORIGIN}/api/auth/oidc/callback?code=mock-authorization-code&state=${state}`,
  );

  expect(response.status()).toBe(400);
});

test("callback with duplicate state parameters is rejected", async ({
  page,
}) => {
  const state = randomBytes(32).toString("base64url");

  const response = await page.request.get(
    `${APP_ORIGIN}/api/auth/oidc/callback?code=mock-authorization-code&state=${state}&state=${state}`,
  );

  expect(response.status()).toBe(400);
});

test("callback rejects ambiguous or malformed code and error parameters", async ({
  browser,
}) => {
  const scenarios: Array<{
    name: string;
    params: Array<[string, string]>;
  }> = [
    {
      name: "missing code and error",
      params: [],
    },
    {
      name: "duplicate code",
      params: [
        ["code", "first-code"],
        ["code", "second-code"],
      ],
    },
    {
      name: "code and error together",
      params: [
        ["code", "mock-authorization-code"],
        ["error", "access_denied"],
      ],
    },
    {
      name: "duplicate error",
      params: [
        ["error", "access_denied"],
        ["error", "login_required"],
      ],
    },
    {
      name: "empty authorization code",
      params: [["code", ""]],
    },
    {
      name: "oversized authorization code",
      params: [["code", "A".repeat(8193)]],
    },
    {
      name: "authorization code containing CRLF",
      params: [["code", "bad\r\nvalue"]],
    },
    {
      name: "provider error response",
      params: [["error", "access_denied"]],
    },
  ];

  for (const scenario of scenarios) {
    const page = await browser.newPage();

    try {
      const { state } = await beginLogin(page);

      const callbackUrl = new URL(
        "/api/auth/oidc/callback",
        APP_ORIGIN,
      );

      callbackUrl.searchParams.set("state", state);

      for (const [key, value] of scenario.params) {
        callbackUrl.searchParams.append(key, value);
      }

      const response = await page.goto(callbackUrl.toString(), {
        waitUntil: "domcontentloaded",
      });

      expect(
        response,
        `Callback response must exist for: ${scenario.name}`,
      ).not.toBeNull();

      if (!response) {
        throw new Error(
          `Callback navigation returned no response: ${scenario.name}`,
        );
      }

      expect(
        response.status(),
        `Callback must reject: ${scenario.name}`,
      ).toBe(400);

      const cookies = await page.context().cookies(APP_ORIGIN);

      expect(
        cookies.some(
          (cookie) => cookie.name === browserCookieName(state),
        ),
        `Browser-binding cookie must be cleared: ${scenario.name}`,
      ).toBe(false);
    } finally {
      await page.close();
    }
  }
});

test("callback ignores attacker-controlled redirect parameters", async ({
  page,
}) => {
  const { state } = await beginLogin(page);

  const callbackUrl = new URL(
    "/api/auth/oidc/callback",
    APP_ORIGIN,
  );

  callbackUrl.searchParams.set("code", "mock-authorization-code");
  callbackUrl.searchParams.set("state", state);
  callbackUrl.searchParams.set(
    "returnTo",
    "https://attacker.example/steal",
  );
  callbackUrl.searchParams.set(
    "redirect",
    "https://attacker.example/redirect",
  );
  callbackUrl.searchParams.set("next", "//attacker.example");

  const callbackResponsePromise = page.waitForResponse((response) => {
    const url = new URL(response.url());

    return (
      url.origin === APP_ORIGIN &&
      url.pathname === "/api/auth/oidc/callback" &&
      response.request().method() === "GET"
    );
  });

  await page.goto(callbackUrl.toString(), {
    waitUntil: "domcontentloaded",
  });

  const callbackResponse = await callbackResponsePromise;

  expect(callbackResponse.status()).toBe(303);
  await expect(page).toHaveURL(`${APP_ORIGIN}/`);

  await expect(
    page.getByText("zak-e2e@example.test"),
  ).toBeVisible();
});

test("session cookie helper rejects missing, malformed and duplicate cookies", () => {
  const validToken = "A".repeat(40);

  expect(readSingleSessionToken([])).toBeNull();
  expect(readSingleSessionToken([{ value: "short" }])).toBeNull();
  expect(readSingleSessionToken([{ value: "A".repeat(513) }])).toBeNull();

  // Printable ASCII only: reject whitespace, controls, DEL and Unicode.
  const invalidTokens = [
    " " + validToken,
    validToken + " ",
    validToken.slice(0, 20) + " " + validToken.slice(20),
    validToken + "\t",
    validToken + "\r",
    validToken + "\n",
    validToken + "\0",
    validToken + "\u001F",
    validToken + "\u007F",
    validToken.slice(0, 20) + "\u00E9" + validToken.slice(21),
    validToken.slice(0, 20) + "\uD83D\uDE00" + validToken.slice(21),
  ];

  for (const invalidToken of invalidTokens) {
    expect(
      readSingleSessionToken([{ value: invalidToken }]),
      "Session parser must reject non-printable or non-ASCII tokens",
    ).toBeNull();
  }
  expect(
    readSingleSessionToken([
      { value: validToken },
      { value: validToken },
    ]),
  ).toBeNull();
  expect(readSingleSessionToken([{ value: validToken }])).toBe(validToken);
});

test("internal API URL requires HTTPS except development loopback", () => {
  expect(
    parseInternalApiBaseUrl("https://api.example.test", "production")?.origin,
  ).toBe("https://api.example.test");

  expect(
    parseInternalApiBaseUrl("http://127.0.0.1:8000", "development")?.origin,
  ).toBe("http://127.0.0.1:8000");

  expect(
    parseInternalApiBaseUrl("http://api.example.test", "development"),
  ).toBeNull();

  expect(
    parseInternalApiBaseUrl("http://127.0.0.1:8000", "production"),
  ).toBeNull();

  expect(
    parseInternalApiBaseUrl("file:///tmp/internal-api", "development"),
  ).toBeNull();

  expect(
    parseInternalApiBaseUrl("https://user:pass@api.example.test", "production"),
  ).toBeNull();

  expect(
    parseInternalApiBaseUrl("https://api.example.test/?token=x", "production"),
  ).toBeNull();
});

test("pure internal API URL builder preserves approved base paths", () => {
  const healthUrl = buildInternalApiUrl(
    "/api/v1/market-data/health",
    "GET",
    "http://127.0.0.1:8000/service-prefix/",
    "development",
  );

  expect(healthUrl?.href).toBe(
    "http://127.0.0.1:8000/service-prefix/api/v1/market-data/health",
  );

  const latestUrl = buildInternalApiUrl(
    "/api/v1/market-data/latest/spot/BTCUSDT",
    "GET",
    "https://api.example.test/gateway/v2",
    "production",
  );

  expect(latestUrl?.href).toBe(
    "https://api.example.test/gateway/v2/api/v1/market-data/latest/spot/BTCUSDT",
  );

  expect(latestUrl?.origin).toBe("https://api.example.test");
  expect(latestUrl?.username).toBe("");
  expect(latestUrl?.password).toBe("");
  expect(latestUrl?.search).toBe("");
  expect(latestUrl?.hash).toBe("");
});

test("pure internal API URL builder fails closed for invalid paths and configuration", () => {
  const invalidCases: Array<[string, string, string | undefined, string | undefined]> = [
    ["//attacker.example/path", "GET", "http://127.0.0.1:8000", "development"],
    ["/api/v1/market-data/health?debug=true", "GET", "http://127.0.0.1:8000", "development"],
    ["/api/v1/market-data/health", "POST", "http://127.0.0.1:8000", "development"],
    ["/api/v1/market-data/latest/other/BTCUSDT", "GET", "http://127.0.0.1:8000", "development"],
    ["/api/v1/market-data/health", "GET", "http://api.example.test", "development"],
    ["/api/v1/market-data/health", "GET", "", "development"],
  ];

  for (const [path, method, configured, appEnv] of invalidCases) {
    expect(
      buildInternalApiUrl(path, method, configured, appEnv),
      `${method} ${path} must fail closed`,
    ).toBeNull();
  }
});
test("internal API endpoint allowlist accepts only approved paths and methods", () => {
  const allowed: Array<[string, string]> = [
    ["/internal/v1/auth/oidc/transactions", "POST"],
    ["/internal/v1/auth/oidc/callback", "POST"],
    ["/internal/v1/auth/session/revoke", "POST"],
    ["/internal/v1/auth/session/validate", "POST"],
    ["/api/v1/market-data/health", "GET"],
    ["/api/v1/market-data/latest/spot/BTCUSDT", "GET"],
    ["/api/v1/market-data/latest/futures_mark_price/ETHUSDT", "GET"],
    ["/api/v1/market-data/latest/spot/A1", "GET"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/manual-top-up", "POST"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/reset", "POST"],
  ];

  for (const [path, method] of allowed) {
    expect(
      isAllowedInternalApiRequest(path, method),
      `${method} ${path} should be allowed`,
    ).toBe(true);
  }

  expect(
    isAllowedInternalApiRequest(
      "/internal/v1/auth/session/validate",
      "post",
    ),
  ).toBe(true);
});

test("internal API endpoint allowlist rejects unapproved methods and routes", () => {
  const invalid: Array<[string, string]> = [
    ["/internal/v1/auth/oidc/transactions", "GET"],
    ["/internal/v1/auth/oidc/callback", "GET"],
    ["/internal/v1/auth/session/revoke", "GET"],
    ["/internal/v1/auth/session/validate", "GET"],
    ["/api/v1/market-data/health", "POST"],
    ["/api/v1/market-data/latest/spot/BTCUSDT", "POST"],
    ["/api/v1/market-data/latest/futures_mark_price/BTCUSDT", "DELETE"],
    ["/internal/v1/auth/admin", "POST"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/manual-top-up", "GET"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/manual-top-up/extra", "POST"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/manual-top-up?debug=true", "POST"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/reset", "GET"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/reset/extra", "POST"],
    ["/api/v1/accounts/11111111-1111-4111-8111-111111111111/portfolios/22222222-2222-4222-8222-222222222222/reset?debug=true", "POST"],
    ["/api/v1/accounts/not-a-uuid/portfolios/22222222-2222-4222-8222-222222222222/reset", "POST"],
    ["/api/v1/accounts/not-a-uuid/portfolios/22222222-2222-4222-8222-222222222222/manual-top-up", "POST"],
    ["/api/v1/market-data/private", "GET"],
    ["/api/v1/market-data/latest/other/BTCUSDT", "GET"],
    ["/api/v1/market-data/latest/spot/btcusdt", "GET"],
    ["/api/v1/market-data/latest/spot/A", "GET"],
    ["/api/v1/market-data/latest/spot/" + "A".repeat(31), "GET"],
    ["/api/v1/market-data/latest/spot/BTC_USDT", "GET"],
    ["/api/v1/market-data/latest/spot/BTC-USDT", "GET"],
    ["api/v1/market-data/health", "GET"],
    ["//api/v1/market-data/health", "GET"],
    ["/api/v1/market-data/health?debug=true", "GET"],
    ["/api/v1/market-data/health#fragment", "GET"],
    ["/api/v1/market-data/health\\extra", "GET"],
    ["/api/v1/market-data/health\r\nX-Test: injected", "GET"],
    ["/api/v1/market-data/latest/spot%2FBTCUSDT", "GET"],
    ["/api/v1/market-data/latest/spot/%2F", "GET"],
    ["/api/v1/market-data/latest/spot/%5C", "GET"],
    ["/api/v1/market-data/latest/spot/%2E%2E", "GET"],
    ["/api/v1/market-data/latest/spot/BTCUSDT", "GET\nPOST"],
    ["/api/v1/market-data/health", ""],
  ];

  for (const [path, method] of invalid) {
    expect(
      isAllowedInternalApiRequest(path, method),
      `${JSON.stringify(method)} ${JSON.stringify(path)} should be rejected`,
    ).toBe(false);
  }
});
test("mock internal API rejects requests without BFF service authentication", async ({
  page,
}) => {
  const response = await page.request.post(
    `${MOCK_ORIGIN}/internal/v1/auth/session/validate`,
    {
      data: {
        session_token: "A".repeat(40),
      },
    },
  );

  expect(response.status()).toBe(401);
});

test("BFF rejects internal redirects without following them", async ({
  page,
}) => {
  const enableResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/redirect-internal?enabled=true`,
  );
  expect(enableResponse.ok()).toBe(true);

  const loginResponse = await page.request.post(
    `${APP_ORIGIN}/api/auth/oidc/login`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
      },
    },
  );

  expect(loginResponse.status()).toBe(503);

  const countResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/redirect-target-count`,
  );

  expect(countResponse.ok()).toBe(true);
  expect(await countResponse.json()).toEqual({ hits: 0 });
});
test("revoked backend session is rejected while browser cookie remains", async ({
  page,
}) => {
  const token = await establishBrowserSession(page);

  const revokeResponse = await page.request.post(
    `${MOCK_ORIGIN}/__test__/session-state`,
    {
      data: {
        session_token: token,
        action: "revoke",
      },
    },
  );

  expect(revokeResponse.status()).toBe(200);

  await page.goto("/markets", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/login$/);

  await expect(
    page.getByRole("heading", { name: "Sign in" }),
  ).toBeVisible();

  const cookies = await page.context().cookies(APP_ORIGIN);

  expect(
    cookies.some((cookie) => cookie.name === SESSION_COOKIE_NAME),
    "The browser cookie remains present; backend revocation must independently deny access.",
  ).toBe(true);
});

test("expired backend session is rejected while browser cookie remains", async ({
  page,
}) => {
  const token = await establishBrowserSession(page);

  const expireResponse = await page.request.post(
    `${MOCK_ORIGIN}/__test__/session-state`,
    {
      data: {
        session_token: token,
        action: "expire",
      },
    },
  );

  expect(expireResponse.status()).toBe(200);

  await page.goto("/markets", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/login$/);

  await expect(
    page.getByRole("heading", { name: "Sign in" }),
  ).toBeVisible();

  const cookies = await page.context().cookies(APP_ORIGIN);

  expect(
    cookies.some((cookie) => cookie.name === SESSION_COOKIE_NAME),
    "The browser cookie remains present; backend expiry must independently deny access.",
  ).toBe(true);
});

test("session validation service unavailable fails closed", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const modeResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/session-validation-mode?mode=unavailable`,
  );

  expect(modeResponse.status()).toBe(200);

  try {
    const response = await page.goto("/markets", {
      waitUntil: "domcontentloaded",
    });

    expect(
      response?.status(),
      "An unavailable authentication service must not return protected content successfully.",
    ).toBe(500);

    await expect(
      page.getByText("zak-e2e@example.test"),
    ).toHaveCount(0);
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/session-validation-mode?mode=valid`,
    );

    expect(resetResponse.status()).toBe(200);
  }
});

test("malformed successful session validation response fails closed", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const modeResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/session-validation-mode?mode=malformed`,
  );

  expect(modeResponse.status()).toBe(200);

  try {
    const response = await page.goto("/markets", {
      waitUntil: "domcontentloaded",
    });

    expect(
      response?.status(),
      "Malformed authentication data must never produce a successful protected response.",
    ).toBe(500);

    expect(page.url()).toBe(`${APP_ORIGIN}/markets`);

    await expect(
      page.getByText("zak-e2e@example.test"),
    ).toHaveCount(0);

    const cookies = await page.context().cookies(APP_ORIGIN);

    expect(
      cookies.some((cookie) => cookie.name === SESSION_COOKIE_NAME),
      "The invalid validation response must not authenticate the browser or silently alter its cookie.",
    ).toBe(true);
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/session-validation-mode?mode=valid`,
    );

    expect(resetResponse.status()).toBe(200);
  }
});

test("market data BFF authenticates upstream requests without exposing credentials to the browser", async ({
  page,
}) => {
  const browserInternalAuthHeaders: string[] = [];

  page.on("request", (request) => {
    const url = new URL(request.url());

    if (
      url.origin === APP_ORIGIN &&
      url.pathname.startsWith("/api/market-data/")
    ) {
      const headers = request.headers();

      if (headers["x-zak-internal-service-secret"]) {
        browserInternalAuthHeaders.push(
          headers["x-zak-internal-service-secret"],
        );
      }
    }
  });

  await page.goto("/login", { waitUntil: "domcontentloaded" });

  const browserResults = await page.evaluate(async () => {
    const healthResponse = await fetch("/api/market-data/health", {
      method: "GET",
      cache: "no-store",
    });

    const healthBody = await healthResponse.json();

    const latestResponse = await fetch(
      "/api/market-data/latest/spot/BTCUSDT",
      {
        method: "GET",
        cache: "no-store",
      },
    );

    const latestBody = await latestResponse.json();

    return {
      health: {
        status: healthResponse.status,
        cacheControl: healthResponse.headers.get("cache-control"),
        internalSecretHeader: healthResponse.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: healthBody,
      },
      latest: {
        status: latestResponse.status,
        cacheControl: latestResponse.headers.get("cache-control"),
        internalSecretHeader: latestResponse.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: latestBody,
      },
    };
  });

  expect(browserResults.health.status).toBe(200);
  expect(browserResults.health.body).toEqual({ status: "ok" });
  expect(browserResults.health.cacheControl).toBe("no-store");
  expect(browserResults.health.internalSecretHeader).toBeNull();

  expect(browserResults.latest.status).toBe(200);
  expect(browserResults.latest.body).toEqual({
    group: "spot",
    symbol: "BTCUSDT",
    source: "mock",
  });
  expect(browserResults.latest.cacheControl).toBe("no-store");
  expect(browserResults.latest.internalSecretHeader).toBeNull();

  expect(browserInternalAuthHeaders).toEqual([]);

  const directUnauthenticatedRequest = await page.request.get(
    `${MOCK_ORIGIN}/api/v1/market-data/health`,
  );

  expect(directUnauthenticatedRequest.status()).toBe(401);
});

test("market data BFF preserves upstream 401 and 503 responses", async ({
  page,
}) => {
  await page.goto("/login", { waitUntil: "domcontentloaded" });

  const modes = [
    { mode: "unauthorized", expectedStatus: 401 },
    { mode: "unavailable", expectedStatus: 503 },
  ];

  try {
    for (const item of modes) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/market-data-mode?mode=${item.mode}`,
      );

      expect(modeResponse.status()).toBe(200);

      const results = await page.evaluate(async () => {
        const health = await fetch("/api/market-data/health", {
          cache: "no-store",
        });

        const latest = await fetch(
          "/api/market-data/latest/spot/BTCUSDT",
          { cache: "no-store" },
        );

        return {
          healthStatus: health.status,
          latestStatus: latest.status,
          healthCache: health.headers.get("cache-control"),
          latestCache: latest.headers.get("cache-control"),
        };
      });

      expect(results.healthStatus).toBe(item.expectedStatus);
      expect(results.latestStatus).toBe(item.expectedStatus);
      expect(results.healthCache).toBe("no-store");
      expect(results.latestCache).toBe("no-store");
    }
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/market-data-mode?mode=valid`,
    );

    expect(resetResponse.status()).toBe(200);
  }
});

test("market data BFF rejects unsupported groups and malformed symbols", async ({
  page,
}) => {
  await page.goto("/login", { waitUntil: "domcontentloaded" });

  const results = await page.evaluate(async () => {
    const unsupportedGroup = await fetch(
      "/api/market-data/latest/unknown/BTCUSDT",
    );

    const lowercaseSymbol = await fetch(
      "/api/market-data/latest/spot/btcusdt",
    );

    const invalidCharacters = await fetch(
      "/api/market-data/latest/spot/BTC%2FUSDT",
    );

    return {
      unsupportedGroup: unsupportedGroup.status,
      lowercaseSymbol: lowercaseSymbol.status,
      invalidCharacters: invalidCharacters.status,
    };
  });

  expect(results.unsupportedGroup).toBe(404);
  expect(results.lowercaseSymbol).toBe(404);
  expect(results.invalidCharacters).toBe(404);
});


const PAPER_SPOT_ACCOUNT_ID = "11111111-1111-4111-8111-111111111111";
const PAPER_SPOT_PORTFOLIO_ID = "22222222-2222-4222-8222-222222222222";
const PAPER_RESET_PATH =
  `/api/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/reset`;
const PAPER_SPOT_PATH =
  `/api/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/spot`;
const PAPER_BOOTSTRAP_PATH = "/api/paper-trading/bootstrap";

async function submitBrowserSpotTrade(
  page: Page,
  body: Record<string, unknown>,
  idempotencyKey = randomUUID(),
) {
  return page.evaluate(
    async ({ path, payload, key }) => {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(payload),
        cache: "no-store",
      });

      return {
        status: response.status,
        cacheControl: response.headers.get("cache-control"),
        internalSecretHeader: response.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: await response.json(),
      };
    },
    {
      path: PAPER_SPOT_PATH,
      payload: body,
      key: idempotencyKey,
    },
  );
}

const PAPER_MANUAL_TOP_UP_PATH =
  `/api/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/manual-top-up`;

async function submitBrowserManualTopUp(
  page: Page,
  body: unknown = { confirmed: true },
  idempotencyKey: string = randomUUID(),
) {
  return page.evaluate(
    async ({ path, payload, key }) => {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(payload),
        cache: "no-store",
      });

      return {
        status: response.status,
        cacheControl: response.headers.get("cache-control"),
        internalSecretHeader: response.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: await response.json(),
      };
    },
    {
      path: PAPER_MANUAL_TOP_UP_PATH,
      payload: body,
      key: idempotencyKey,
    },
  );
}
async function submitBrowserPortfolioReset(
  page: Page,
  body: unknown = { confirmed: true },
  idempotencyKey: string = randomUUID(),
) {
  return page.evaluate(
    async ({ path, payload, key }) => {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(payload),
        cache: "no-store",
      });

      const responseText = await response.text();
      let responseBody: unknown = null;

      try {
        responseBody = JSON.parse(responseText);
      } catch {
        responseBody = null;
      }

      return {
        status: response.status,
        cacheControl: response.headers.get("cache-control"),
        internalSecretHeader: response.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: responseBody,
      };
    },
    {
      path: PAPER_RESET_PATH,
      payload: body,
      key: idempotencyKey,
    },
  );
}
const PAPER_FUTURES_PORTFOLIO_ID = "55555555-5555-4555-8555-555555555555";
const PAPER_FUTURES_PATH =
  `/api/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_FUTURES_PORTFOLIO_ID}/paper-trades/futures`;

async function submitBrowserFuturesTrade(
  page: Page,
  body: Record<string, unknown>,
  idempotencyKey: string = randomUUID(),
) {
  return page.evaluate(
    async ({ path, payload, key }) => {
      const response = await fetch(path, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify(payload),
        cache: "no-store",
      });

      return {
        status: response.status,
        cacheControl: response.headers.get("cache-control"),
        internalSecretHeader: response.headers.get(
          "x-zak-internal-service-secret",
        ),
        body: await response.json(),
      };
    },
    {
      path: PAPER_FUTURES_PATH,
      payload: body,
      key: idempotencyKey,
    },
  );
}
test("APP_PUBLIC_URL permits HTTP loopback only in development", () => {
  expect(
    parseAppPublicOrigin(
      "http://localhost:3000",
      "development",
    )?.origin,
  ).toBe("http://localhost:3000");

  expect(
    parseAppPublicOrigin(
      "http://127.0.0.1:3000",
      "development",
    )?.origin,
  ).toBe("http://127.0.0.1:3000");

  expect(
    parseAppPublicOrigin(
      "http://localhost:3000",
      "production",
    ),
  ).toBeNull();

  expect(
    parseAppPublicOrigin(
      "http://api.example.test",
      "development",
    ),
  ).toBeNull();

  expect(
    parseAppPublicOrigin(
      "https://api.example.test",
      "production",
    )?.origin,
  ).toBe("https://api.example.test");

  expect(
    parseAppPublicOrigin(
      "https://user:pass@api.example.test",
      "production",
    ),
  ).toBeNull();

  expect(
    parseAppPublicOrigin(
      "https://api.example.test/path",
      "production",
    ),
  ).toBeNull();
});

test("Spot internal API allowlist accepts only exact UUID route POST", () => {
  const validPath =
    `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
    `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/spot`;

  expect(isAllowedInternalApiRequest(validPath, "POST")).toBe(true);
  expect(isAllowedInternalApiRequest(validPath, "post")).toBe(true);
  expect(isAllowedInternalApiRequest(validPath, "GET")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath + "/extra", "POST")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath + "?debug=true", "POST")).toBe(false);
  expect(
    isAllowedInternalApiRequest(
      validPath.replace(PAPER_SPOT_ACCOUNT_ID, "not-a-uuid"),
      "POST",
    ),
  ).toBe(false);
});

test("Futures internal API allowlist accepts only exact UUID route POST", () => {
  const validPath =
    `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
    `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/futures`;

  expect(isAllowedInternalApiRequest(validPath, "POST")).toBe(true);
  expect(isAllowedInternalApiRequest(validPath, "post")).toBe(true);
  expect(isAllowedInternalApiRequest(validPath, "GET")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath, "PUT")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath, "DELETE")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath + "/extra", "POST")).toBe(false);
  expect(isAllowedInternalApiRequest(validPath + "?debug=true", "POST")).toBe(false);
  expect(
    isAllowedInternalApiRequest(
      validPath.replace(PAPER_SPOT_ACCOUNT_ID, "not-a-uuid"),
      "POST",
    ),
  ).toBe(false);

  const spotPath =
    `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
    `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/spot`;

  expect(isAllowedInternalApiRequest(spotPath, "POST")).toBe(true);
});
test("Paper Trading bootstrap internal API allowlist accepts only exact GET", () => {
  expect(
    isAllowedInternalApiRequest("/api/v1/paper-trading/bootstrap", "GET"),
  ).toBe(true);
  expect(
    isAllowedInternalApiRequest("/api/v1/paper-trading/bootstrap", "POST"),
  ).toBe(false);
  expect(
    isAllowedInternalApiRequest("/api/v1/paper-trading/bootstrap/", "GET"),
  ).toBe(false);
  expect(
    isAllowedInternalApiRequest("/api/v1/paper-trading/bootstrap?debug=true", "GET"),
  ).toBe(false);
});

test("Paper Trading bootstrap requires authentication and returns no-store authoritative snapshot", async ({
  page,
}) => {
  const unauthenticated = await page.request.get(
    `${APP_ORIGIN}${PAPER_BOOTSTRAP_PATH}`,
  );
  expect(unauthenticated.status()).toBe(401);
  expect(unauthenticated.headers()["cache-control"]).toBe("no-store");

  await establishBrowserSession(page);

  const result = await page.evaluate(async (path) => {
    const response = await fetch(path, { method: "GET", cache: "no-store" });
    return {
      status: response.status,
      cacheControl: response.headers.get("cache-control"),
      internalSecretHeader: response.headers.get("x-zak-internal-service-secret"),
      body: await response.json(),
    };
  }, PAPER_BOOTSTRAP_PATH);

  expect(result.status).toBe(200);
  expect(result.cacheControl).toBe("no-store");
  expect(result.internalSecretHeader).toBeNull();
  expect(result.body.account.role).toBe("owner");
  expect(result.body.portfolios.map((item: { market_type: string }) => item.market_type))
    .toEqual(["spot", "futures"]);
  expect(result.body.portfolios[0].ledger.cash_available).toBe("1000.00");

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-captures`,
  );
  expect(captureResponse.status()).toBe(200);
  expect((await captureResponse.json()).captures).toEqual([
    {
      path: "/api/v1/paper-trading/bootstrap",
      sessionAuthenticated: true,
    },
  ]);
});

test("Paper Trading bootstrap maps upstream failures and rejects malformed snapshots", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const cases = [
    { mode: "unauthorized", expected: 401 },
    { mode: "forbidden", expected: 403 },
    { mode: "not-found", expected: 404 },
    { mode: "conflict", expected: 409 },
    { mode: "unavailable", expected: 503 },
    { mode: "malformed", expected: 503 },
  ];

  try {
    for (const item of cases) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=${item.mode}`,
      );
      expect(modeResponse.status()).toBe(200);

      const response = await page.evaluate(async (path) => {
        const result = await fetch(path, { method: "GET", cache: "no-store" });
        return {
          status: result.status,
          cacheControl: result.headers.get("cache-control"),
          body: await result.json(),
        };
      }, PAPER_BOOTSTRAP_PATH);

      expect(response.status, item.mode).toBe(item.expected);
      expect(response.cacheControl).toBe("no-store");
    }
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    await page.request.get(`${MOCK_ORIGIN}/__test__/paper-bootstrap-captures`);
  }
});

test("Spot BFF rejects missing, null, mismatched Origin and unsafe Fetch Metadata", async ({
  page,
}) => {
  const body = JSON.stringify({
    symbol: "BTCUSDT",
    side: "buy",
    quantity: "0.01",
  });

  const cases: Array<{ name: string; headers: Record<string, string> }> = [
    {
      name: "missing Origin",
      headers: {
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
      },
    },
    {
      name: "null Origin",
      headers: {
        origin: "null",
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
      },
    },
    {
      name: "cross-origin Origin",
      headers: {
        origin: "https://attacker.example",
        "content-type": "application/json",
        "sec-fetch-site": "cross-site",
      },
    },
    {
      name: "same-site Fetch Metadata",
      headers: {
        origin: APP_ORIGIN,
        "content-type": "application/json",
        "sec-fetch-site": "same-site",
      },
    },
    {
      name: "none Fetch Metadata",
      headers: {
        origin: APP_ORIGIN,
        "content-type": "application/json",
        "sec-fetch-site": "none",
      },
    },
  ];

  for (const scenario of cases) {
    const response = await page.request.post(
      `${APP_ORIGIN}${PAPER_SPOT_PATH}`,
      {
        headers: scenario.headers,
        data: body,
      },
    );

    expect(
      response.status(),
      scenario.name,
    ).toBe(403);
  }
});

test("Spot BFF rejects unauthenticated trade requests", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}${PAPER_SPOT_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": randomUUID(),
      },
      data: {
        symbol: "BTCUSDT",
        side: "buy",
        quantity: "0.01",
      },
    },
  );

  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");
});

test("Spot BFF forwards authenticated trade through server-only credentials", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const browserServiceSecrets: string[] = [];

  page.on("request", (request) => {
    const url = new URL(request.url());

    if (url.origin === APP_ORIGIN && url.pathname === PAPER_SPOT_PATH) {
      const headers = request.headers();

      if (headers["x-zak-internal-service-secret"]) {
        browserServiceSecrets.push(
          headers["x-zak-internal-service-secret"],
        );
      }
    }
  });

  const key = randomUUID();

  const result = await submitBrowserSpotTrade(
    page,
    {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0.01",
    },
    key,
  );

  expect(result.status).toBe(200);
  expect(result.cacheControl).toBe("no-store");
  expect(result.internalSecretHeader).toBeNull();
  expect(result.body.status).toBe("accepted");
  expect(result.body.replayed).toBe(false);

  expect(browserServiceSecrets).toEqual([]);

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
  );

  expect(captureResponse.status()).toBe(200);

  const capturePayload = await captureResponse.json();

  expect(capturePayload.captures).toHaveLength(1);

  expect(capturePayload.captures[0]).toMatchObject({
    path:
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
      `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/spot`,
    body: {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0.01",
      position_id: null,
    },
    idempotencyKey: key,
    sessionAuthenticated: true,
  });
});

test("Spot BFF rejects invalid trade payloads before backend forwarding", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const invalidBodies = [
    {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0.01",
      position_id: "33333333-3333-4333-8333-333333333333",
    },
    {
      symbol: "BTCUSDT",
      side: "sell",
      quantity: "0.01",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "1e-8",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0.01",
      unexpected: true,
    },
  ];

  for (const body of invalidBodies) {
    const result = await submitBrowserSpotTrade(page, body);
    expect(result.status).toBe(422);
    expect(result.cacheControl).toBe("no-store");
  }

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
  );

  expect((await captureResponse.json()).captures).toHaveLength(0);
});

test("Spot BFF maps upstream authorization and execution failures safely", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const modes = [
    { mode: "unauthorized", expectedStatus: 401 },
    { mode: "forbidden", expectedStatus: 403 },
    { mode: "not-found", expectedStatus: 404 },
    { mode: "conflict", expectedStatus: 409 },
    { mode: "unprocessable", expectedStatus: 422 },
    { mode: "unavailable", expectedStatus: 503 },
  ];

  try {
    for (const item of modes) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-spot-mode?mode=${item.mode}`,
      );

      expect(modeResponse.status()).toBe(200);

      const result = await submitBrowserSpotTrade(page, {
        symbol: "BTCUSDT",
        side: "buy",
        quantity: "0.01",
      });

      expect(
        result.status,
        `Spot BFF mapping for ${item.mode}`,
      ).toBe(item.expectedStatus);

      expect(result.cacheControl).toBe("no-store");
      expect(result.internalSecretHeader).toBeNull();
    }

    const malformedMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-spot-mode?mode=malformed`,
    );

    expect(malformedMode.status()).toBe(200);

    const malformedResult = await submitBrowserSpotTrade(page, {
      symbol: "BTCUSDT",
      side: "buy",
      quantity: "0.01",
    });

    expect(malformedResult.status).toBe(503);
    expect(malformedResult.cacheControl).toBe("no-store");
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-spot-mode?mode=valid`,
    );

    expect(resetResponse.status()).toBe(200);

    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
    );
  }
});

test("Manual Top-up BFF rejects unsafe Origin and Fetch Metadata", async ({
  page,
}) => {
  const cases: Array<{ name: string; headers: Record<string, string> }> = [
    {
      name: "missing Origin",
      headers: {
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
      },
    },
    {
      name: "cross-origin Origin",
      headers: {
        origin: "https://attacker.example",
        "content-type": "application/json",
        "sec-fetch-site": "cross-site",
      },
    },
    {
      name: "same-site Fetch Metadata",
      headers: {
        origin: APP_ORIGIN,
        "content-type": "application/json",
        "sec-fetch-site": "same-site",
      },
    },
  ];

  for (const scenario of cases) {
    const response = await page.request.post(
      `${APP_ORIGIN}${PAPER_MANUAL_TOP_UP_PATH}`,
      {
        headers: scenario.headers,
        data: { confirmed: true },
      },
    );

    expect(response.status(), scenario.name).toBe(403);
    expect(response.headers()["cache-control"]).toBe("no-store");
  }

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Portfolio Reset BFF rejects unauthenticated requests", async ({ page }) => {
  await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );

  const response = await page.request.post(
    `${APP_ORIGIN}${PAPER_RESET_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": randomUUID(),
      },
      data: { confirmed: true },
    },
  );

  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Portfolio Reset BFF forwards authenticated confirmation and supports idempotent replay", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );
  expect(fixture.status()).toBe(200);

  const key = randomUUID();

  const first = await submitBrowserPortfolioReset(
    page,
    { confirmed: true },
    key,
  );

  expect(first.status).toBe(200);
  expect(first.cacheControl).toBe("no-store");
  expect(first.internalSecretHeader).toBeNull();
  expect(first.body).toMatchObject({
    operation_type: "reset",
    status: "completed",
    account_id: PAPER_SPOT_ACCOUNT_ID,
    portfolio_id: PAPER_SPOT_PORTFOLIO_ID,
    market_type: "spot",
    previous_session_number: 1,
    new_session_number: 2,
    initial_balance: "1000.00",
    replayed: false,
  });

  const second = await submitBrowserPortfolioReset(
    page,
    { confirmed: true },
    key,
  );

  expect(second.status).toBe(200);
  expect(second.cacheControl).toBe("no-store");
  expect(second.internalSecretHeader).toBeNull();
  expect(second.body).toMatchObject({
    operation_type: "reset",
    status: "completed",
    new_session_id: (first.body as Record<string, unknown>).new_session_id,
    new_book_id: (first.body as Record<string, unknown>).new_book_id,
    new_session_number: 2,
    replayed: true,
  });

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
  );
  expect(captureResponse.status()).toBe(200);

  const captures = (await captureResponse.json()).captures;
  expect(captures).toHaveLength(2);

  expect(captures[0]).toMatchObject({
    path:
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
      `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/reset`,
    body: { confirmed: true },
    idempotencyKey: key,
    sessionAuthenticated: true,
  });

  expect(captures[1].idempotencyKey).toBe(key);
});

test("Portfolio Reset BFF rejects invalid bodies and idempotency keys before forwarding", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );
  expect(fixture.status()).toBe(200);

  const invalidBodies: unknown[] = [
    { confirmed: false },
    { confirmed: "true" },
    { confirmed: true, unexpected: true },
    [],
    null,
    "confirmed",
  ];

  for (const body of invalidBodies) {
    const result = await submitBrowserPortfolioReset(page, body);

    expect(result.status, JSON.stringify(body)).toBe(422);
    expect(result.cacheControl).toBe("no-store");
    expect(result.internalSecretHeader).toBeNull();
  }

  const missingKey = await page.request.post(
    `${APP_ORIGIN}${PAPER_RESET_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
      },
      data: { confirmed: true },
    },
  );

  expect(missingKey.status()).toBe(400);

  const invalidKey = await submitBrowserPortfolioReset(
    page,
    { confirmed: true },
    "not-a-uuid",
  );

  expect(invalidKey.status).toBe(400);
  expect(invalidKey.cacheControl).toBe("no-store");

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
  );
  expect((await captureResponse.json()).captures).toHaveLength(0);
});

test("Portfolio Reset BFF maps upstream failures safely", async ({ page }) => {
  await establishBrowserSession(page);

  const modes = [
    { mode: "unauthorized", expectedStatus: 401 },
    { mode: "forbidden", expectedStatus: 403 },
    { mode: "not-found", expectedStatus: 404 },
    { mode: "conflict", expectedStatus: 409 },
    { mode: "unprocessable", expectedStatus: 422 },
    { mode: "unavailable", expectedStatus: 503 },
  ];

  try {
    for (const item of modes) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=${item.mode}`,
      );
      expect(modeResponse.status()).toBe(200);

      const result = await submitBrowserPortfolioReset(page);

      expect(
        result.status,
        `Portfolio Reset BFF mapping for ${item.mode}`,
      ).toBe(item.expectedStatus);

      expect(result.cacheControl).toBe("no-store");
      expect(result.internalSecretHeader).toBeNull();
    }

    const malformedMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=malformed`,
    );
    expect(malformedMode.status()).toBe(200);

    const malformedResult = await submitBrowserPortfolioReset(page);

    expect(malformedResult.status).toBe(503);
    expect(malformedResult.cacheControl).toBe("no-store");
    expect(malformedResult.internalSecretHeader).toBeNull();
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    expect(resetResponse.status()).toBe(200);

    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
  }
});
test("Manual Top-up BFF rejects unauthenticated requests", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}${PAPER_MANUAL_TOP_UP_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": randomUUID(),
      },
      data: { confirmed: true },
    },
  );

  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Manual Top-up BFF forwards authenticated confirmation through server-only credentials", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const resetCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect(resetCaptures.status()).toBe(200);

  const bootstrapMode = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=manual-top-up-eligible`,
  );
  expect(bootstrapMode.status()).toBe(200);

  const key = randomUUID();
  const result = await submitBrowserManualTopUp(
    page,
    { confirmed: true },
    key,
  );

  expect(result.status).toBe(200);
  expect(result.cacheControl).toBe("no-store");
  expect(result.internalSecretHeader).toBeNull();
  expect(result.body.transaction_type).toBe("manual_top_up");
  expect(result.body.replayed).toBe(false);

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect(captureResponse.status()).toBe(200);

  const captures = (await captureResponse.json()).captures;
  expect(captures).toHaveLength(1);
  expect(captures[0]).toMatchObject({
    path:
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
      `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/manual-top-up`,
    body: { confirmed: true },
    idempotencyKey: key,
    sessionAuthenticated: true,
  });
});

test("Manual Top-up BFF rejects invalid bodies and idempotency keys before forwarding", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const resetCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect(resetCaptures.status()).toBe(200);

  const invalidBodies: unknown[] = [
    { confirmed: false },
    { confirmed: "true" },
    { confirmed: true, unexpected: true },
    [],
    null,
    "confirmed",
  ];

  for (const body of invalidBodies) {
    const result = await submitBrowserManualTopUp(page, body);
    expect(result.status, JSON.stringify(body)).toBe(422);
    expect(result.cacheControl).toBe("no-store");
  }

  const body = { confirmed: true };

  const missingKey = await page.request.post(
    `${APP_ORIGIN}${PAPER_MANUAL_TOP_UP_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
      },
      data: body,
    },
  );
  expect(missingKey.status()).toBe(400);

  const invalidKey = await submitBrowserManualTopUp(page, body, "not-a-uuid");
  expect(invalidKey.status).toBe(400);

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Manual Top-up BFF maps upstream failures safely", async ({ page }) => {
  await establishBrowserSession(page);

  const modes = [
    { mode: "unauthorized", expectedStatus: 401 },
    { mode: "forbidden", expectedStatus: 403 },
    { mode: "not-found", expectedStatus: 404 },
    { mode: "conflict", expectedStatus: 409 },
    { mode: "unprocessable", expectedStatus: 422 },
    { mode: "unavailable", expectedStatus: 503 },
  ];

  try {
    for (const item of modes) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-manual-top-up-mode?mode=${item.mode}`,
      );
      expect(modeResponse.status()).toBe(200);

      const result = await submitBrowserManualTopUp(page);

      expect(
        result.status,
        `Manual Top-up BFF mapping for ${item.mode}`,
      ).toBe(item.expectedStatus);
      expect(result.cacheControl).toBe("no-store");
      expect(result.internalSecretHeader).toBeNull();
    }

    const malformedMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-mode?mode=malformed`,
    );
    expect(malformedMode.status()).toBe(200);

    const malformedResult = await submitBrowserManualTopUp(page);
    expect(malformedResult.status).toBe(503);
    expect(malformedResult.cacheControl).toBe("no-store");
    expect(malformedResult.internalSecretHeader).toBeNull();
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-mode?mode=valid`,
    );
    expect(resetResponse.status()).toBe(200);

    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
  }
});
test("Futures BFF rejects unsafe Origin and Fetch Metadata", async ({ page }) => {
  const body = {
    symbol: "BTCUSDT",
    side: "buy",
    position_side: "long",
    quantity: "0.01",
    leverage: "10",
  };

  const cases: Array<{ name: string; headers: Record<string, string> }> = [
    {
      name: "missing Origin",
      headers: {
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
      },
    },
    {
      name: "null Origin",
      headers: {
        origin: "null",
        "content-type": "application/json",
        "sec-fetch-site": "same-origin",
      },
    },
    {
      name: "cross-origin Origin",
      headers: {
        origin: "https://attacker.example",
        "content-type": "application/json",
        "sec-fetch-site": "cross-site",
      },
    },
    {
      name: "same-site Fetch Metadata",
      headers: {
        origin: APP_ORIGIN,
        "content-type": "application/json",
        "sec-fetch-site": "same-site",
      },
    },
    {
      name: "none Fetch Metadata",
      headers: {
        origin: APP_ORIGIN,
        "content-type": "application/json",
        "sec-fetch-site": "none",
      },
    },
  ];

  for (const scenario of cases) {
    const response = await page.request.post(
      `${APP_ORIGIN}${PAPER_FUTURES_PATH}`,
      {
        headers: scenario.headers,
        data: body,
      },
    );

    expect(response.status(), scenario.name).toBe(403);
    expect(response.headers()["cache-control"]).toBe("no-store");
  }

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Futures BFF rejects unauthenticated trade requests", async ({ page }) => {
  const response = await page.request.post(
    `${APP_ORIGIN}${PAPER_FUTURES_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": "00000000-0000-4000-8000-000000000000",
      },
      data: {
        symbol: "BTCUSDT",
        side: "buy",
        position_side: "long",
        quantity: "0.01",
        leverage: "10",
      },
    },
  );

  expect(response.status()).toBe(401);
  expect(response.headers()["cache-control"]).toBe("no-store");

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Futures BFF forwards authenticated Futures intent with server-only credentials", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const modeResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-mode?mode=valid`,
  );
  expect(modeResponse.status()).toBe(200);

  const browserServiceSecrets: string[] = [];

  page.on("request", (request) => {
    const url = new URL(request.url());

    if (url.origin === APP_ORIGIN && url.pathname === PAPER_FUTURES_PATH) {
      const headers = request.headers();

      if (headers["x-zak-internal-service-secret"]) {
        browserServiceSecrets.push(
          headers["x-zak-internal-service-secret"],
        );
      }
    }
  });

  const key = randomUUID();

  const result = await submitBrowserFuturesTrade(
    page,
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
    },
    key,
  );

  expect(result.status).toBe(200);
  expect(result.cacheControl).toBe("no-store");
  expect(result.internalSecretHeader).toBeNull();
  expect(result.body.status).toBe("posted");
  expect(result.body.market_type).toBe("futures");
  expect(result.body.quote_source).toBe("futures_depth_book");
  expect(result.body.replayed).toBe(false);
  expect(browserServiceSecrets).toEqual([]);

  const captureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect(captureResponse.status()).toBe(200);

  const captures = (await captureResponse.json()).captures;
  expect(captures).toHaveLength(1);
  expect(captures[0]).toMatchObject({
    path:
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
      `/portfolios/${PAPER_FUTURES_PORTFOLIO_ID}/paper-trades/futures`,
    body: {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      order_type: "market",
      position_id: null,
      reduce_only: false,
      limit_price: null,
    },
    idempotencyKey: key,
    sessionAuthenticated: true,
  });
});

test("Futures BFF rejects invalid Futures intent before backend forwarding", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const invalidBodies: Array<Record<string, unknown>> = [
    {
      symbol: "BTCUSDT",
      side: "sell",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "short",
      quantity: "0.01",
      leverage: "10",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0",
      leverage: "10",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "1e-8",
      leverage: "10",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "0",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "1e2",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      reduce_only: true,
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      reduce_only: true,
      position_id: "33333333-3333-4333-8333-333333333333",
    },
    {
      symbol: "BTCUSDT",
      side: "sell",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      reduce_only: true,
      position_id: "not-a-uuid",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      order_type: "limit",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      limit_price: "100",
    },
    {
      symbol: "BTCUSDT",
      side: "buy",
      position_side: "long",
      quantity: "0.01",
      leverage: "10",
      unexpected: true,
    },
  ];

  for (const body of invalidBodies) {
    const result = await submitBrowserFuturesTrade(page, body);
    expect(result.status, JSON.stringify(body)).toBe(422);
    expect(result.cacheControl).toBe("no-store");
  }

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect((await captures.json()).captures).toHaveLength(0);
});

test("Futures BFF enforces Idempotency-Key boundaries", async ({ page }) => {
  await establishBrowserSession(page);

  const body = {
    symbol: "BTCUSDT",
    side: "buy",
    position_side: "long",
    quantity: "0.01",
    leverage: "10",
  };

  const missingKey = await page.request.post(
    `${APP_ORIGIN}${PAPER_FUTURES_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
      },
      data: body,
    },
  );
  expect(missingKey.status()).toBe(400);
  expect(missingKey.headers()["cache-control"]).toBe("no-store");

  const emptyKey = await page.request.post(
    `${APP_ORIGIN}${PAPER_FUTURES_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": "",
      },
      data: body,
    },
  );
  expect(emptyKey.status()).toBe(400);

  const oversizedKey = await page.request.post(
    `${APP_ORIGIN}${PAPER_FUTURES_PATH}`,
    {
      headers: {
        origin: APP_ORIGIN,
        "sec-fetch-site": "same-origin",
        "content-type": "application/json",
        "Idempotency-Key": "K".repeat(129),
      },
      data: body,
    },
  );
  expect(oversizedKey.status()).toBe(400);

  const oneCharacterKey = await submitBrowserFuturesTrade(page, body, "K");
  expect(oneCharacterKey.status).toBe(400);

  const maximumKey = await submitBrowserFuturesTrade(
    page,
    body,
    "M".repeat(128),
  );
  expect(maximumKey.status).toBe(400);

  const validKey = randomUUID();
  const validKeyResponse = await submitBrowserFuturesTrade(page, body, validKey);
  expect(validKeyResponse.status).toBe(200);

  const captures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  const captured = (await captures.json()).captures;

  expect(captured).toHaveLength(1);
  expect(captured[0].idempotencyKey).toBe(validKey);
});

test("Futures BFF maps upstream authorization and execution failures safely", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const body = {
    symbol: "BTCUSDT",
    side: "buy",
    position_side: "long",
    quantity: "0.01",
    leverage: "10",
  };

  const modes = [
    { mode: "unauthorized", expectedStatus: 401 },
    { mode: "forbidden", expectedStatus: 403 },
    { mode: "not-found", expectedStatus: 404 },
    { mode: "conflict", expectedStatus: 409 },
    { mode: "unprocessable", expectedStatus: 422 },
    { mode: "unavailable", expectedStatus: 503 },
  ];

  try {
    for (const item of modes) {
      const modeResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-futures-mode?mode=${item.mode}`,
      );
      expect(modeResponse.status()).toBe(200);

      const result = await submitBrowserFuturesTrade(page, body);

      expect(
        result.status,
        `Futures BFF mapping for ${item.mode}`,
      ).toBe(item.expectedStatus);

      expect(result.cacheControl).toBe("no-store");
      expect(result.internalSecretHeader).toBeNull();
    }

    const malformedMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-futures-mode?mode=malformed`,
    );
    expect(malformedMode.status()).toBe(200);

    const malformedResult = await submitBrowserFuturesTrade(page, body);
    expect(malformedResult.status).toBe(503);
    expect(malformedResult.cacheControl).toBe("no-store");
    expect(malformedResult.internalSecretHeader).toBeNull();
  } finally {
    const resetResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-futures-mode?mode=valid`,
    );
    expect(resetResponse.status()).toBe(200);

    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
    );
  }
});



test("Paper Trading UI resets Spot and verifies the refreshed session and ledger", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const spotButton = page.getByRole("button", {
      name: /Reset Spot - Session 1/,
    });

    await expect(spotButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("Reset Spot portfolio?");
      expect(dialog.message()).toContain("Current session: 1");
      expect(dialog.message()).toContain(
        "33333333-3333-4333-8333-333333333333",
      );
      await dialog.accept();
    });

    await spotButton.click();

    await expect(
      page.getByText(/Spot portfolio reset verified\./),
    ).toBeVisible();

    await expect(
      page.getByRole("button", { name: /Reset Spot - Session 2/ }),
    ).toBeEnabled();

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
    expect(capturesResponse.status()).toBe(200);

    const captures = (await capturesResponse.json()).captures;

    expect(captures).toHaveLength(1);
    expect(captures[0]).toMatchObject({
      path:
        `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
        `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/reset`,
      body: { confirmed: true },
      sessionAuthenticated: true,
    });
    expect(captures[0].idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
  }
});

test("Paper Trading UI resets Futures independently", async ({ page }) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const futuresButton = page.getByRole("button", {
      name: /Reset Futures - Session 1/,
    });

    await expect(futuresButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("Reset Futures portfolio?");
      expect(dialog.message()).toContain("Current session: 1");
      await dialog.accept();
    });

    await futuresButton.click();

    await expect(
      page.getByText(/Futures portfolio reset verified\./),
    ).toBeVisible();

    await expect(
      page.getByRole("button", { name: /Reset Futures - Session 2/ }),
    ).toBeEnabled();

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
    expect(capturesResponse.status()).toBe(200);

    const captures = (await capturesResponse.json()).captures;

    expect(captures).toHaveLength(1);
    expect(captures[0]).toMatchObject({
      path:
        `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
        `/portfolios/${PAPER_FUTURES_PORTFOLIO_ID}/reset`,
      body: { confirmed: true },
      sessionAuthenticated: true,
    });
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
  }
});

test("Paper Trading UI does not reset a portfolio when confirmation is cancelled", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const spotButton = page.getByRole("button", {
      name: /Reset Spot - Session 1/,
    });

    await expect(spotButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("Reset Spot portfolio?");
      await dialog.dismiss();
    });

    await spotButton.click();

    await expect(spotButton).toBeEnabled();

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
    expect(capturesResponse.status()).toBe(200);
    expect((await capturesResponse.json()).captures).toHaveLength(0);
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
  }
});

test("Paper Trading UI retains the same idempotency key after an uncertain Reset outcome", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=unavailable`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const spotButton = page.getByRole("button", {
      name: /Reset Spot - Session 1/,
    });

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      await dialog.accept();
    });

    await spotButton.click();

    await expect(
      page.getByText(/Spot Reset outcome requires verification/),
    ).toBeVisible();

    const firstCaptureResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
    const firstCaptures = (await firstCaptureResponse.json()).captures;

    expect(firstCaptures).toHaveLength(1);

    const firstKey = firstCaptures[0].idempotencyKey;
    expect(firstKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );

    const retryMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    expect(retryMode.status()).toBe(200);

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      await dialog.accept();
    });

    await spotButton.click();

    await expect(
      page.getByText(/Spot portfolio reset verified\./),
    ).toBeVisible();

    const secondCaptureResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
    const secondCaptures = (await secondCaptureResponse.json()).captures;

    expect(secondCaptures).toHaveLength(1);
    expect(secondCaptures[0].idempotencyKey).toBe(firstKey);
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-reset-captures`,
    );
  }
});

test("Paper Trading UI confirms and posts independent Spot and Futures Manual Top-ups", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const resetCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );
  expect(resetCaptures.status()).toBe(200);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=manual-top-up-eligible`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const spotButton = page.getByRole("button", {
      name: /Manual Top-up Spot/,
    });
    const futuresButton = page.getByRole("button", {
      name: /Manual Top-up Futures/,
    });

    await expect(spotButton).toBeEnabled();
    await expect(futuresButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("Spot Manual Top-up");
      await dialog.accept();
    });

    await spotButton.click();

    await expect(
      page.getByText(/Spot Manual Top-up posted:/),
    ).toBeVisible();

    await expect(spotButton).toBeDisabled();
    await expect(futuresButton).toBeEnabled();

    const spotCapturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    const spotCaptures = (await spotCapturesResponse.json()).captures;

    expect(spotCaptures).toHaveLength(1);
    expect(spotCaptures[0]).toMatchObject({
      path:
        `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
        `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/manual-top-up`,
      body: { confirmed: true },
      sessionAuthenticated: true,
    });

    const resetFixture = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=manual-top-up-eligible`,
    );
    expect(resetFixture.status()).toBe(200);

    const resetCaptures = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    expect(resetCaptures.status()).toBe(200);

    await page.reload({ waitUntil: "domcontentloaded" });

    const refreshedFuturesButton = page.getByRole("button", {
      name: /Manual Top-up Futures/,
    });
    await expect(refreshedFuturesButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      expect(dialog.message()).toContain("Futures Manual Top-up");
      await dialog.accept();
    });

    await refreshedFuturesButton.click();

    await expect(
      page.getByText(/Futures Manual Top-up posted:/),
    ).toBeVisible();

    const futuresCapturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    const futuresCaptures = (await futuresCapturesResponse.json()).captures;

    expect(futuresCaptures).toHaveLength(1);
    expect(futuresCaptures[0]).toMatchObject({
      path:
        `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
        `/portfolios/55555555-5555-4555-8555-555555555555/manual-top-up`,
      body: { confirmed: true },
      sessionAuthenticated: true,
    });
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
  }
});

test("Paper Trading UI does not submit Manual Top-up when confirmation is cancelled", async ({
  page,
}) => {
  await establishBrowserSession(page);

  await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
  );

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=manual-top-up-eligible`,
  );
  expect(fixture.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    const spotButton = page.getByRole("button", {
      name: /Manual Top-up Spot/,
    });
    await expect(spotButton).toBeEnabled();

    page.once("dialog", async (dialog) => {
      expect(dialog.type()).toBe("confirm");
      await dialog.dismiss();
    });

    await spotButton.click();

    await expect(spotButton).toBeEnabled();

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    expect((await capturesResponse.json()).captures).toHaveLength(0);
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
  }
});

test("Paper Trading UI restricts Manual Top-up to Owner and Admin roles", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const modes = [
    { mode: "manual-top-up-admin", enabled: true },
    { mode: "manual-top-up-trader", enabled: false },
    { mode: "manual-top-up-viewer", enabled: false },
  ];

  try {
    for (const item of modes) {
      const fixture = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=${item.mode}`,
      );
      expect(fixture.status(), item.mode).toBe(200);

      await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

      const spotButton = page.getByRole("button", {
        name: /Manual Top-up Spot/,
      });
      const futuresButton = page.getByRole("button", {
        name: /Manual Top-up Futures/,
      });

      if (item.enabled) {
        await expect(spotButton, item.mode).toBeEnabled();
        await expect(futuresButton, item.mode).toBeEnabled();
      } else {
        await expect(spotButton, item.mode).toBeDisabled();
        await expect(futuresButton, item.mode).toBeDisabled();
      }
    }

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    expect((await capturesResponse.json()).captures).toHaveLength(0);
  } finally {
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
  }
});
test("Paper Trading UI blocks Manual Top-up for over-limit and disabled portfolios independently", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const cases = [
    {
      mode: "manual-top-up-over-limit-spot",
      spotEnabled: false,
      futuresEnabled: true,
    },
    {
      mode: "manual-top-up-over-limit-futures",
      spotEnabled: true,
      futuresEnabled: false,
    },
    {
      mode: "manual-top-up-disabled-spot",
      spotEnabled: false,
      futuresEnabled: true,
    },
    {
      mode: "manual-top-up-disabled-futures",
      spotEnabled: true,
      futuresEnabled: false,
    },
  ];

  try {
    for (const item of cases) {
      const resetCaptures = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
      );
      expect(resetCaptures.status(), item.mode).toBe(200);

      const fixture = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=${item.mode}`,
      );
      expect(fixture.status(), item.mode).toBe(200);

      await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

      const spotButton = page.getByRole("button", {
        name: /Manual Top-up Spot/,
      });
      const futuresButton = page.getByRole("button", {
        name: /Manual Top-up Futures/,
      });

      if (item.spotEnabled) {
        await expect(spotButton, item.mode).toBeEnabled();
      } else {
        await expect(spotButton, item.mode).toBeDisabled();
      }

      if (item.futuresEnabled) {
        await expect(futuresButton, item.mode).toBeEnabled();
      } else {
        await expect(futuresButton, item.mode).toBeDisabled();
      }

      const capturesResponse = await page.request.get(
        `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
      );
      expect(capturesResponse.status(), item.mode).toBe(200);
      expect(
        (await capturesResponse.json()).captures,
        item.mode,
      ).toHaveLength(0);
    }
  } finally {
    const resetMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    expect(resetMode.status()).toBe(200);

    const resetCaptures = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-manual-top-up-captures`,
    );
    expect(resetCaptures.status()).toBe(200);
  }
});

test("Paper Trading UI submits Spot Buy and Sell orders", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const initialCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
  );
  expect(initialCaptures.status()).toBe(200);
  expect((await initialCaptures.json()).captures).toHaveLength(0);

  const fixture = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=spot-positions`,
  );
  expect(fixture.status()).toBe(200);

  const spotPositionId = "99999999-9999-4999-8999-999999999991";

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    await expect(
      page.getByRole("heading", { name: "Paper Trading" }),
    ).toBeVisible();

    const sideField = page.getByRole("combobox", { name: "Side", exact: true });
    const spotForm = page.locator("form.paper-trade-form").filter({ has: sideField });

    await expect(spotForm).toBeVisible();

    await spotForm.getByLabel("Symbol").fill("BTCUSDT");
    await spotForm.getByLabel("Quantity").fill("0.005");

    // Spot Buy through the actual browser form.
    const buyResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.origin === APP_ORIGIN &&
        url.pathname === PAPER_SPOT_PATH &&
        response.request().method() === "POST"
      );
    });

    await spotForm.getByRole("button", { name: "Place Spot Buy" }).click();

    const buyResponse = await buyResponsePromise;
    expect(buyResponse.status()).toBe(200);

    await expect(
      page.getByText(/Trade accepted by the backend/),
    ).toBeVisible();

    // Switch to Sell and select the known open Spot position.
    await sideField.selectOption("sell");

    const positionField = spotForm.getByRole("combobox", { name: "Open Spot position",
      exact: true,
    });

    await expect(positionField).toBeVisible();
    await expect(positionField).toBeEnabled();
    await positionField.selectOption(spotPositionId);

    await spotForm.getByLabel("Symbol").fill("BTCUSDT");
    await spotForm.getByLabel("Quantity").fill("0.005");

    const sellResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.origin === APP_ORIGIN &&
        url.pathname === PAPER_SPOT_PATH &&
        response.request().method() === "POST"
      );
    });

    await spotForm.getByRole("button", { name: "Place Spot Sell" }).click();

    const sellResponse = await sellResponsePromise;
    expect(sellResponse.status()).toBe(200);

    await expect(
      page.getByText(/Trade accepted by the backend/),
    ).toBeVisible();

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
    );
    expect(capturesResponse.status()).toBe(200);

    const captures = (await capturesResponse.json()).captures;
    expect(captures).toHaveLength(2);

    const expectedPath =
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}` +
      `/portfolios/${PAPER_SPOT_PORTFOLIO_ID}/paper-trades/spot`;

    expect(captures[0]).toMatchObject({
      path: expectedPath,
      body: {
        symbol: "BTCUSDT",
        side: "buy",
        quantity: "0.005",
      },
      sessionAuthenticated: true,
    });
    expect(captures[0].body).toHaveProperty("position_id", null);

    expect(captures[1]).toMatchObject({
      path: expectedPath,
      body: {
        symbol: "BTCUSDT",
        side: "sell",
        quantity: "0.005",
        position_id: spotPositionId,
      },
      sessionAuthenticated: true,
    });

    for (const capture of captures) {
      expect(capture.idempotencyKey).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
    }

    expect(captures[0].idempotencyKey).not.toBe(
      captures[1].idempotencyKey,
    );
  } finally {
    const resetMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    expect(resetMode.status()).toBe(200);

    const resetCaptures = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-spot-captures`,
    );
    expect(resetCaptures.status()).toBe(200);
  }
});
test("Paper Trading UI submits Futures Long and Short orders", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const resetCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect(resetCaptures.status()).toBe(200);

  await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

  await expect(
    page.getByRole("heading", { name: "Paper Trading" }),
  ).toBeVisible();

  await expect(page.getByText("Futures market order")).toBeVisible();

  const leverageField = page.getByLabel("Leverage", { exact: true });
  await expect(leverageField).toBeVisible();

  const futuresForm = leverageField.locator("xpath=ancestor::form");
  await expect(futuresForm).toBeVisible();

  await futuresForm.getByLabel("Symbol").fill("BTCUSDT");
  await futuresForm.getByLabel("Leverage").fill("5");

  const expectedOrders = [
    { direction: "long", orderSide: "buy" },
    { direction: "short", orderSide: "sell" },
  ];

  for (const order of expectedOrders) {
    await futuresForm.getByLabel("Position direction").selectOption(
      order.direction,
    );

    await futuresForm.getByLabel("Quantity").fill("0.01");

    const responsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.origin === APP_ORIGIN &&
        url.pathname === PAPER_FUTURES_PATH &&
        response.request().method() === "POST"
      );
    });

    await futuresForm
      .getByRole("button", {
        name: `Open Futures ${order.direction.toUpperCase()}`,
      })
      .click();

    const response = await responsePromise;
    expect(response.status()).toBe(200);

    await expect(
      page.getByText(/Futures trade accepted by the backend/),
    ).toBeVisible();
  }

  const capturesResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect(capturesResponse.status()).toBe(200);

  const captures = (await capturesResponse.json()).captures;
  expect(captures).toHaveLength(2);

  for (let index = 0; index < expectedOrders.length; index += 1) {
    const expected = expectedOrders[index];
    const captured = captures[index];

    expect(captured.path).toBe(
      `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_FUTURES_PORTFOLIO_ID}/paper-trades/futures`,
    );
    expect(captured.sessionAuthenticated).toBe(true);
    expect(captured.body).toEqual({
      symbol: "BTCUSDT",
      side: expected.orderSide,
      position_side: expected.direction,
      quantity: "0.01",
      leverage: "5",
      order_type: "market",
      position_id: null,
      reduce_only: false,
      limit_price: null,
    });
    expect(captured.idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  }
});


test("Paper Trading UI reduces existing Futures Long and Short positions", async ({
  page,
}) => {
  await establishBrowserSession(page);

  const resetCaptures = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
  );
  expect(resetCaptures.status()).toBe(200);

  const fixtureResponse = await page.request.get(
    `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=futures-positions`,
  );
  expect(fixtureResponse.status()).toBe(200);

  try {
    await page.goto("/paper-trading", { waitUntil: "domcontentloaded" });

    await expect(
      page.getByRole("heading", { name: "Paper Trading" }),
    ).toBeVisible();

    const leverageField = page.getByLabel("Leverage", { exact: true });
    await expect(leverageField).toBeVisible();

    const futuresForm = leverageField.locator("xpath=ancestor::form");
    await expect(futuresForm).toBeVisible();

    await futuresForm.getByLabel("Action").selectOption("reduce");

    const expectedReductions = [
      {
        positionId: "88888888-8888-4888-8888-888888888881",
        symbol: "BTCUSDT",
        positionSide: "long",
        orderSide: "sell",
        quantity: "0.005",
        leverage: "5",
      },
      {
        positionId: "88888888-8888-4888-8888-888888888882",
        symbol: "ETHUSDT",
        positionSide: "short",
        orderSide: "buy",
        quantity: "0.10",
        leverage: "3",
      },
    ];

    for (const reduction of expectedReductions) {
      const positionSelect = page.getByRole("combobox", {
        name: "Open Futures position",
        exact: true,
      });

      await expect(positionSelect).toBeEnabled();
      await positionSelect.selectOption(reduction.positionId);

      await expect(futuresForm.getByLabel("Symbol")).toHaveValue(
        reduction.symbol,
      );
      await expect(futuresForm.getByLabel("Leverage")).toHaveValue(
        reduction.leverage,
      );

      await futuresForm.getByLabel("Quantity").fill(reduction.quantity);

      const responsePromise = page.waitForResponse((response) => {
        const url = new URL(response.url());
        return (
          url.origin === APP_ORIGIN &&
          url.pathname === PAPER_FUTURES_PATH &&
          response.request().method() === "POST"
        );
      });

      await futuresForm
        .getByRole("button", {
          name: "Reduce / Close Futures Position",
        })
        .click();

      const response = await responsePromise;
      expect(response.status()).toBe(200);

      await expect(
        page.getByText(/Futures trade accepted by the backend/),
      ).toBeVisible();
    }

    const capturesResponse = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
    );
    expect(capturesResponse.status()).toBe(200);

    const captures = (await capturesResponse.json()).captures;
    expect(captures).toHaveLength(2);

    for (
      let index = 0;
      index < expectedReductions.length;
      index += 1
    ) {
      const expected = expectedReductions[index];
      const captured = captures[index];

      expect(captured.path).toBe(
        `/api/v1/accounts/${PAPER_SPOT_ACCOUNT_ID}/portfolios/${PAPER_FUTURES_PORTFOLIO_ID}/paper-trades/futures`,
      );
      expect(captured.sessionAuthenticated).toBe(true);
      expect(captured.body).toEqual({
        symbol: expected.symbol,
        side: expected.orderSide,
        position_side: expected.positionSide,
        quantity: expected.quantity,
        leverage: expected.leverage,
        order_type: "market",
        position_id: expected.positionId,
        reduce_only: true,
        limit_price: null,
      });
      expect(captured.idempotencyKey).toMatch(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
      );
    }
  } finally {
    const resetMode = await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-bootstrap-mode?mode=valid`,
    );
    expect(resetMode.status()).toBe(200);

    await page.request.get(
      `${MOCK_ORIGIN}/__test__/paper-futures-captures`,
    );
  }
});


