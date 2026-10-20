import { NextRequest, NextResponse } from "next/server";
import { authFailure } from "@/lib/auth";

function sameOrigin(request: NextRequest, origin: string): boolean {
  try {
    const source = new URL(origin).origin;
    if (source === request.nextUrl.origin) return true;
    // The reverse proxy may terminate HTTPS and rewrite the upstream host.
    const host = request.headers.get("x-forwarded-host")?.split(",")[0].trim() ?? request.nextUrl.host;
    const proto = request.headers.get("x-forwarded-proto")?.split(",")[0].trim() ?? request.nextUrl.protocol.slice(0, -1);
    return source === new URL(`${proto}://${host}`).origin;
  } catch {
    return false;
  }
}

async function forward(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  const denied = await authFailure(request);
  if (denied) return denied;
  const origin = request.headers.get("origin");
  if (!["GET", "HEAD"].includes(request.method) && origin !== null && !sameOrigin(request, origin)) {
    return NextResponse.json({ error: "Cross-origin requests are not allowed." }, { status: 403 });
  }
  const { path } = await context.params;
  if (!path.length || path.some((part) => !part || part === "." || part === ".." || /[%/\\\x00-\x1f]/.test(part))) {
    return NextResponse.json({ error: "Invalid API path." }, { status: 400 });
  }
  const base = process.env.NEXT_PUBLIC_ARGUS_API;
  if (!base) return NextResponse.json({ error: "NEXT_PUBLIC_ARGUS_API is not configured." }, { status: 503 });

  // Append encoded segments to the configured base, never resolve a user-supplied URL.
  const target = new URL(`${base.replace(/\/+$/, "")}/${path.map(encodeURIComponent).join("/")}`);
  target.search = request.nextUrl.search;
  const headers = new Headers();
  const hopHeaders = request.headers.get("connection")?.toLowerCase().split(",").map((name) => name.trim()) ?? [];
  for (const name of ["content-type", "accept"]) {
    const value = request.headers.get(name);
    if (value && !hopHeaders.includes(name)) headers.set(name, value);
  }
  if (process.env.ARGUS_API_TOKEN) headers.set("authorization", `Bearer ${process.env.ARGUS_API_TOKEN}`);

  const timeout = AbortSignal.timeout(120_000);
  const signal = AbortSignal.any([request.signal, timeout]);
  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      // Redirects must not send credentials to another destination.
      redirect: "manual",
      signal,
    });
    const responseHeaders = new Headers({ "cache-control": "no-store" });
    const connection = upstream.headers.get("connection")?.toLowerCase().split(",").map((name) => name.trim()) ?? [];
    const contentType = upstream.headers.get("content-type");
    // Only the body type is needed; cookies and hop-by-hop headers stay upstream.
    if (contentType && !connection.includes("content-type")) responseHeaders.set("content-type", contentType);
    const body = upstream.body ? await upstream.arrayBuffer() : null;
    return new Response(body, { status: upstream.status, headers: responseHeaders });
  } catch {
    if (request.signal.aborted && signal.reason === request.signal.reason) {
      return new Response(null, { status: 499 });
    }
    if (timeout.aborted) {
      return NextResponse.json({ error: "The API timed out." }, { status: 504 });
    }
    return NextResponse.json({ error: "The API is unavailable." }, { status: 502 });
  }
}

export { forward as GET, forward as HEAD, forward as POST, forward as PUT, forward as PATCH, forward as DELETE, forward as OPTIONS };
