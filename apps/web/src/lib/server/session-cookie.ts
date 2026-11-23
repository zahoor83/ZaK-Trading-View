export const SESSION_COOKIE_NAME = "__Host-zak_session";
export const LEGACY_SESSION_COOKIE_NAME = "zak_session";

export function readSingleSessionToken(
  cookieValues: readonly { value: unknown }[],
): string | null {
  if (!Array.isArray(cookieValues) || cookieValues.length !== 1) {
    return null;
  }

  const token = cookieValues[0]?.value;

  if (
    typeof token !== "string" ||
    token.length < 32 ||
    token.length > 512 ||
    !/^[\x21-\x7E]+$/.test(token)
  ) {
    return null;
  }

  return token;
}