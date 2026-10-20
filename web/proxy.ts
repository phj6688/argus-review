import { NextRequest, NextResponse } from "next/server";
import { authFailure, configurationError } from "@/lib/auth";

export async function proxy(request: NextRequest) {
  if (process.env.ARGUS_AUTH_MODE === "proxy") return NextResponse.next();
  const unavailable = configurationError();
  if (unavailable) return unavailable;

  const path = request.nextUrl.pathname;
  if (path === "/login" || path === "/api/login" || path.startsWith("/_next/static/") ||
      ["/favicon.ico", "/file.svg", "/globe.svg", "/next.svg", "/vercel.svg", "/window.svg"].includes(path)) {
    return NextResponse.next();
  }

  const denied = await authFailure(request);
  if (denied) {
    if (path === "/api" || path.startsWith("/api/") || path.startsWith("/_next/") ||
        !["GET", "HEAD"].includes(request.method) || request.headers.has("rsc") ||
        request.headers.get("accept")?.includes("application/json")) return denied;
    return NextResponse.redirect(new URL("/login", request.url));
  }
  const response = NextResponse.next();
  response.headers.set("cache-control", "no-store");
  return response;
}
