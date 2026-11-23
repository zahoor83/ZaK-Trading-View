import { NextResponse } from "next/server";

import { internalApiFetch } from "@/lib/server/internal-api";
import { validateOidcTransactionResponse } from "@/lib/server/oidc-login-validation";

export const dynamic = "force-dynamic";
export const runtime = "nodejs";

function unavailable(): NextResponse {
  return NextResponse.json(
    { detail: "Authentication service unavailable." },
    {
      status: 503,
      headers: {
        "Cache-Control": "no-store",
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
      (parsed.protocol !== "https:" && !(parsed.protocol === "http:" && localHost)) ||
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

export async function POST(request: Request): Promise<NextResponse> {
  const publicUrlValue = process.env.APP_PUBLIC_URL;
  const appEnv = process.env.APP_ENV?.trim().toLowerCase();

  if (appEnv !== "development" || !publicUrlValue) {
    return unavailable();
  }

  const publicUrl = parsePublicOrigin(publicUrlValue);
  if (!publicUrl) {
    return unavailable();
  }

  const origin = request.headers.get("origin");
  if (!origin || origin !== publicUrl.origin) {
    return NextResponse.json(
      { detail: "Request origin rejected." },
      {
        status: 403,
        headers: { "Cache-Control": "no-store" },
      },
    );
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && fetchSite !== "same-origin") {
    return NextResponse.json(
      { detail: "Request origin rejected." },
      {
        status: 403,
        headers: { "Cache-Control": "no-store" },
      },
    );
  }

  try {
    const requestUrl = new URL(request.url);
    if (requestUrl.search || requestUrl.hash) {
      return unavailable();
    }

    const auth0Domain = process.env.AUTH0_DOMAIN?.trim();
    const clientId = process.env.AUTH0_CLIENT_ID;
    const callbackUrl = process.env.AUTH0_CALLBACK_URL;

    if (!auth0Domain || !clientId || !callbackUrl) {
      return unavailable();
    }

    let parsedCallbackUrl: URL;

    try {
      parsedCallbackUrl = new URL(callbackUrl);
    } catch {
      return unavailable();
    }

    if (
      parsedCallbackUrl.origin !== publicUrl.origin ||
      parsedCallbackUrl.pathname !== "/api/auth/oidc/callback" ||
      parsedCallbackUrl.search !== "" ||
      parsedCallbackUrl.hash !== "" ||
      parsedCallbackUrl.username !== "" ||
      parsedCallbackUrl.password !== ""
    ) {
      return unavailable();
    }

    const requestId = crypto.randomUUID();

    const upstream = await internalApiFetch(
      "/internal/v1/auth/oidc/transactions",
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ request_id: requestId }),
      },
    );

    if (upstream.status !== 201) {
      return unavailable();
    }

    const payload: unknown = await upstream.json();

    const validated = validateOidcTransactionResponse(payload, {
      auth0Domain,
      clientId,
      callbackUrl,
    });

    const response = NextResponse.redirect(
      validated.authorizationUrl,
      303,
    );

    response.cookies.set({
      name: validated.cookie.name,
      value: validated.cookie.value,
      httpOnly: true,
      secure: true,
      sameSite: "lax",
      path: "/",
      maxAge: validated.cookie.max_age_seconds,
    });

    response.headers.set("Cache-Control", "no-store");
    response.headers.set("Referrer-Policy", "no-referrer");

    return response;
  } catch {
    return unavailable();
  }
}
