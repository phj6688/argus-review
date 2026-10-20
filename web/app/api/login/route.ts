import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE, configurationError, cookieOptions, cookieValue, validCookie } from "@/lib/auth";

export async function POST(request: NextRequest) {
  const unavailable = configurationError();
  if (unavailable) return unavailable;
  if (process.env.ARGUS_AUTH_MODE === "proxy") {
    return new NextResponse(null, { status: 303, headers: { location: "/" } });
  }
  let supplied: FormDataEntryValue | null;
  try {
    supplied = (await request.formData()).get("token");
  } catch {
    return NextResponse.json({ error: "Invalid login form." }, { status: 400 });
  }
  // Comparing signatures keeps both the submitted token and cookie checks in Web Crypto.
  const value = typeof supplied === "string" && supplied ? await cookieValue(supplied) : "";
  if (!await validCookie(value, process.env.ARGUS_API_TOKEN!)) {
    return new NextResponse(null, {
      status: 303, headers: { location: "/login?error=invalid", "cache-control": "no-store" },
    });
  }
  const response = new NextResponse(null, {
    status: 303, headers: { location: "/", "cache-control": "no-store" },
  });
  response.cookies.set(AUTH_COOKIE, value, cookieOptions(request));
  return response;
}
