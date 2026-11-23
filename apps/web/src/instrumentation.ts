/**
 * Next.js server startup security validation.
 *
 * The server must not accept requests unless its internal API
 * transport is configured for the explicitly declared environment.
 */

export async function register(): Promise<void> {
  // This transport is Node.js server-only.
  if (process.env.NEXT_RUNTIME !== "nodejs") {
    return;
  }

  const appEnv = process.env.APP_ENV?.trim().toLowerCase();

  const {
    getInternalServiceTransport,
    registerProductionInternalServiceTransport,
  } = await import("./lib/server/internal-api-transport");

  if (appEnv === "development") {
    // Preserve the existing development-only local transport.
    getInternalServiceTransport();
    return;
  }

  if (appEnv !== "production") {
    throw new Error("Internal service transport is unavailable.");
  }

  // Production always uses the built-in HMAC transport.
  // Secrets are loaded only from protected server-side environment config.
  const { buildProductionInternalServiceTransport } = await import(
    "./lib/server/hmac-internal-api-transport"
  );

  const transport = buildProductionInternalServiceTransport();

  registerProductionInternalServiceTransport(transport);

  // Verify the registered transport before the server accepts requests.
  getInternalServiceTransport();
}