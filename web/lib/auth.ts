import { NextRequest, NextResponse } from "next/server";

export const AUTH_COOKIE = "argus-console";
const label = new TextEncoder().encode("argus-console-login-v1");

async function cookieKey(token: string) {
  return crypto.subtle.importKey(
    "raw", new TextEncoder().encode(token),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"],
  );
}

export async function cookieValue(token: string): Promise<string> {
  const signature = await crypto.subtle.sign("HMAC", await cookieKey(token), label);
  return Array.from(new Uint8Array(signature), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function validCookie(value: string, token: string): Promise<boolean> {
  if (!/^[a-f0-9]{64}$/.test(value) || !token) return false;
  const signature = Uint8Array.from(value.match(/../g)!, (byte) => parseInt(byte, 16));
  // Web Crypto verifies the signature without a timing-sensitive string comparison.
  return crypto.subtle.verify("HMAC", await cookieKey(token), signature, label);
}

export function cookieOptions(request: NextRequest) {
  return {
    httpOnly: true,
    sameSite: "strict" as const,
    secure: request.nextUrl.protocol === "https:" ||
      request.headers.get("x-forwarded-proto")?.split(",")[0].trim().toLowerCase() === "https",
    path: "/",
  };
}

export function configurationError(): NextResponse | null {
  if (process.env.ARGUS_AUTH_MODE === "proxy" || (process.env.ARGUS_API_TOKEN?.length ?? 0) >= 32) return null;
  return NextResponse.json({
    error: "ARGUS_API_TOKEN must be at least 32 characters when ARGUS_AUTH_MODE=token (the default). Generate one with openssl rand -hex 32, or set ARGUS_AUTH_MODE=proxy behind an authenticating reverse proxy.",
  }, { status: 503, headers: { "cache-control": "no-store" } });
}

export async function authFailure(request: NextRequest): Promise<NextResponse | null> {
  if (process.env.ARGUS_AUTH_MODE === "proxy") return null;
  const unavailable = configurationError();
  if (unavailable) return unavailable;
  if (await validCookie(request.cookies.get(AUTH_COOKIE)?.value ?? "", process.env.ARGUS_API_TOKEN!)) return null;
  return NextResponse.json({ error: "Sign in to continue." }, {
    status: 401, headers: { "cache-control": "no-store" },
  });
}
