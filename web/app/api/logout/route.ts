import { NextRequest, NextResponse } from "next/server";
import { AUTH_COOKIE, configurationError, cookieOptions } from "@/lib/auth";

export async function POST(request: NextRequest) {
  const unavailable = configurationError();
  if (unavailable) return unavailable;
  const response = new NextResponse(null, {
    status: 303, headers: { location: "/login", "cache-control": "no-store" },
  });
  response.cookies.set(AUTH_COOKIE, "", { ...cookieOptions(request), maxAge: 0 });
  return response;
}
