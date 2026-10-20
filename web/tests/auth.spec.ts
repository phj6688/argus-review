import { createHmac } from "node:crypto";
import { expect, test } from "@playwright/test";
import { NextRequest } from "next/server";
import { POST as login } from "../app/api/login/route";
import { POST as logout } from "../app/api/logout/route";
import { DELETE, GET, POST, PUT } from "../app/api/argus/[...path]/route";
import { proxy } from "../proxy";
import { cookieOptions, cookieValue, validCookie } from "../lib/auth";

const token = "test-console-token-with-at-least-32-characters";
const cookie = createHmac("sha256", token).update("argus-console-login-v1").digest("hex");

test.beforeEach(() => {
  process.env.ARGUS_AUTH_MODE = "token";
  process.env.ARGUS_API_TOKEN = token;
});

test.afterEach(() => {
  delete process.env.ARGUS_AUTH_MODE;
  delete process.env.ARGUS_API_TOKEN;
});

test("cookies reject changed signatures and set Secure only for HTTPS", async () => {
  expect(await cookieValue(token)).toBe(cookie);
  expect(await validCookie("0".repeat(64), token)).toBe(false);
  expect(cookieOptions(new NextRequest("http://localhost/login")).secure).toBe(false);
  expect(cookieOptions(new NextRequest("https://localhost/login")).secure).toBe(true);
});

test("login rejects a wrong token, sets a protected cookie, and logout clears it", async () => {
  const request = (value: string) => new NextRequest("http://localhost/api/login", {
    method: "POST",
    headers: { "x-forwarded-proto": "https" },
    body: new URLSearchParams({ token: value }),
  });

  const denied = await login(request("wrong"));
  expect(denied.headers.get("location")).toBe("/login?error=invalid");
  expect(denied.cookies.get("argus-console")).toBeUndefined();

  const accepted = await login(request(token));
  expect(accepted.status).toBe(303);
  expect(accepted.cookies.get("argus-console")?.value).toBe(cookie);
  expect(accepted.headers.get("set-cookie")).toMatch(/HttpOnly/i);
  expect(accepted.headers.get("set-cookie")).toMatch(/SameSite=strict/i);
  expect(accepted.headers.get("set-cookie")).toMatch(/Secure/i);

  const authenticated = new NextRequest("http://localhost/api/argus/config", {
    headers: { cookie: `argus-console=${cookie}` },
  });
  expect((await proxy(authenticated)).status).toBe(200);
  process.env.ARGUS_API_TOKEN = "rotated-console-token-with-at-least-32-characters";
  expect((await proxy(authenticated)).status).toBe(401);

  const ended = await logout(new NextRequest("http://localhost/api/logout", { method: "POST" }));
  expect(ended.cookies.get("argus-console")?.value).toBe("");
  expect(ended.headers.get("set-cookie")).toMatch(/Max-Age=0/i);
});

test("the gate redirects pages, denies API calls, and allows public assets", async () => {
  expect((await proxy(new NextRequest("http://localhost/reviews"))).headers.get("location")).toBe("http://localhost/login");
  for (const path of ["/api/argus/config", "/api/argus", "/api/unknown"]) {
    const denied = await proxy(new NextRequest(`http://localhost${path}`, {
      headers: { cookie: "argus-console=forged" },
    }));
    expect(denied.status).toBe(401);
    expect(await denied.json()).toHaveProperty("error");
  }
  for (const path of ["/login", "/api/login", "/favicon.ico", "/_next/static/chunk.js"]) {
    expect((await proxy(new NextRequest(`http://localhost${path}`))).status).toBe(200);
  }
});

test("missing configuration denies even login and assets; proxy mode bypasses the gate", async () => {
  delete process.env.ARGUS_API_TOKEN;
  delete process.env.ARGUS_AUTH_MODE;
  for (const path of ["/", "/login", "/api/login", "/api/argus/config", "/_next/static/chunk.js"]) {
    const denied = await proxy(new NextRequest(`http://localhost${path}`));
    expect(denied.status).toBe(503);
    expect(await denied.text()).toMatch(/ARGUS_API_TOKEN.*ARGUS_AUTH_MODE/);
  }
  process.env.ARGUS_AUTH_MODE = "proxy";
  expect((await proxy(new NextRequest("http://localhost/reviews"))).status).toBe(200);
});

test("token mode refuses 31 characters with setup advice and accepts 32", async () => {
  process.env.ARGUS_API_TOKEN = "x".repeat(31);
  const responses = [
    await proxy(new NextRequest("http://localhost/login")),
    await login(new NextRequest("http://localhost/api/login", { method: "POST" })),
    await logout(new NextRequest("http://localhost/api/logout", { method: "POST" })),
    await GET(new NextRequest("http://localhost/api/argus/config"), {
      params: Promise.resolve({ path: ["config"] }),
    }),
  ];
  for (const response of responses) {
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: expect.stringMatching(/ARGUS_API_TOKEN.*32.*openssl rand -hex 32/) });
    expect(response.headers.get("cache-control")).toBe("no-store");
  }
  process.env.ARGUS_API_TOKEN = "x".repeat(32);
  const accepted = await login(new NextRequest("http://localhost/api/login", {
    method: "POST", body: new URLSearchParams({ token: process.env.ARGUS_API_TOKEN }),
  }));
  expect(accepted.status).toBe(303);
  const acceptedCookie = accepted.cookies.get("argus-console")?.value;
  expect(acceptedCookie).toBeTruthy();
  expect((await proxy(new NextRequest("http://localhost/reviews", {
    headers: { cookie: `argus-console=${acceptedCookie}` },
  }))).status).toBe(200);
  process.env.ARGUS_AUTH_MODE = "proxy";
  process.env.ARGUS_API_TOKEN = "x".repeat(31);
  expect((await proxy(new NextRequest("http://localhost/reviews"))).status).toBe(200);
});

for (const [method, handler] of [["POST", POST], ["PUT", PUT], ["DELETE", DELETE]] as const) {
  test(`${method} rejects foreign origins before contacting upstream`, async () => {
    process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000";
    const originalFetch = globalThis.fetch;
    let calls = 0;
    globalThis.fetch = async () => {
      calls += 1;
      return Response.json({ ok: true });
    };
    try {
      for (const origin of ["https://foreign.test", "http://localhost.evil.test", "null", "invalid"]) {
        const response = await handler(new NextRequest("http://localhost/api/argus/config", {
          method, headers: { origin, cookie: `argus-console=${cookie}` },
        }), { params: Promise.resolve({ path: ["config"] }) });
        expect(response.status).toBe(403);
        expect(await response.json()).toHaveProperty("error");
      }
      expect(calls).toBe(0);
    } finally {
      globalThis.fetch = originalFetch;
      delete process.env.NEXT_PUBLIC_ARGUS_API;
    }
  });
}

test("proxy allows same origins, forwarded origins, missing origins, and foreign GET origins", async () => {
  process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000";
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return Response.json({ ok: true });
  };
  const cases: { method: string; headers: Record<string, string> }[] = [
    { method: "POST", headers: { origin: "http://localhost" } },
    { method: "PUT", headers: {} },
    { method: "DELETE", headers: { origin: "http://localhost" } },
    { method: "GET", headers: { origin: "https://foreign.test" } },
    { method: "POST", headers: { origin: "https://console.test", "x-forwarded-host": "console.test", "x-forwarded-proto": "https" } },
    { method: "POST", headers: { origin: "http://console.test:3000", "x-forwarded-host": "console.test:3000" } },
    { method: "PUT", headers: { origin: "https://localhost", "x-forwarded-proto": "https" } },
    { method: "POST", headers: { origin: "http://localhost", "x-forwarded-host": "console.test", "x-forwarded-proto": "https" } },
  ];
  try {
    for (const { method, headers } of cases) {
      const response = await GET(new NextRequest("http://localhost/api/argus/config", {
        method, headers: { ...headers, cookie: `argus-console=${cookie}` },
      }), { params: Promise.resolve({ path: ["config"] }) });
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ ok: true });
    }
    expect(calls).toBe(cases.length);
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.NEXT_PUBLIC_ARGUS_API;
  }
});

test("proxy returns 504 when the upstream deadline expires", async () => {
  process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000";
  const originalFetch = globalThis.fetch;
  const originalTimeout = AbortSignal.timeout;
  const deadline = new AbortController();
  let timeoutMs: number | undefined;
  AbortSignal.timeout = (ms) => {
    timeoutMs = ms;
    return deadline.signal;
  };
  globalThis.fetch = async (_input, init) => new Promise<Response>((resolve, reject) => {
    const signal = init?.signal;
    if (!signal) {
      resolve(Response.json({ ok: true }));
      return;
    }
    signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    deadline.abort(new DOMException("Deadline expired", "TimeoutError"));
  });
  try {
    const response = await GET(new NextRequest("http://localhost/api/argus/config", {
      headers: { cookie: `argus-console=${cookie}` },
    }), { params: Promise.resolve({ path: ["config"] }) });
    expect(response.status).toBe(504);
    expect(await response.json()).toEqual({ error: "The API timed out." });
    expect(timeoutMs).toBe(120_000);
  } finally {
    globalThis.fetch = originalFetch;
    AbortSignal.timeout = originalTimeout;
    delete process.env.NEXT_PUBLIC_ARGUS_API;
  }
});

test("client cancellation aborts upstream without reporting a timeout", async () => {
  process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000";
  const originalFetch = globalThis.fetch;
  const client = new AbortController();
  let cancelled = false;
  globalThis.fetch = async (_input, init) => new Promise<Response>((resolve, reject) => {
    const signal = init?.signal;
    if (!signal) {
      resolve(Response.json({ ok: true }));
      return;
    }
    signal.addEventListener("abort", () => {
      cancelled = true;
      reject(signal.reason);
    }, { once: true });
    client.abort();
  });
  try {
    const response = await GET(new NextRequest("http://localhost/api/argus/config", {
      signal: client.signal, headers: { cookie: `argus-console=${cookie}` },
    }), { params: Promise.resolve({ path: ["config"] }) });
    expect(cancelled).toBe(true);
    expect(response.status).not.toBe(504);
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.NEXT_PUBLIC_ARGUS_API;
  }
});

test("proxy preserves method, body, query and status while filtering headers", async () => {
  process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000/base";
  const originalFetch = globalThis.fetch;
  let forwarded: Request | undefined;
  globalThis.fetch = async (input, init) => {
    expect(init?.redirect).toBe("manual");
    forwarded = new Request(input, init);
    return new Response('{"saved":true}', {
      status: 202,
      headers: {
        "content-type": "application/json",
        "set-cookie": "upstream=secret",
        connection: "x-private",
        "x-private": "hidden",
        "transfer-encoding": "chunked",
      },
    });
  };
  try {
    const result = await PUT(new NextRequest("http://localhost/api/argus/config?full=1", {
      method: "PUT",
      headers: {
        cookie: `argus-console=${cookie}`,
        authorization: "Bearer browser-value",
        "content-type": "application/json",
        accept: "application/json",
        "x-forwarded-host": "attacker.test",
      },
      body: '{"bugCap":3}',
    }), { params: Promise.resolve({ path: ["config"] }) });
    expect(forwarded?.url).toBe("http://api.internal:8000/base/config?full=1");
    expect(forwarded?.method).toBe("PUT");
    expect(await forwarded?.text()).toBe('{"bugCap":3}');
    expect(Object.fromEntries(forwarded!.headers)).toEqual({
      accept: "application/json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
    });
    expect(result.status).toBe(202);
    expect(await result.json()).toEqual({ saved: true });
    for (const header of ["set-cookie", "connection", "x-private", "transfer-encoding"]) {
      expect(result.headers.has(header)).toBe(false);
    }
  } finally {
    globalThis.fetch = originalFetch;
    delete process.env.NEXT_PUBLIC_ARGUS_API;
  }
});

test("proxy rejects traversal and empty segments before contacting the API", async () => {
  for (const path of [[".."], [""], ["reviews", ""], ["."], ["a/b"], ["a\\b"], ["%2e%2e"]]) {
    const response = await GET(new NextRequest("http://localhost/api/argus/reviews", {
      headers: { cookie: `argus-console=${cookie}` },
    }), { params: Promise.resolve({ path }) });
    expect(response.status).toBe(400);
  }
  expect((await GET(new NextRequest("http://localhost/api/argus/reviews"), {
    params: Promise.resolve({ path: ["reviews"] }),
  })).status).toBe(401);
});

test("server reads send the token and all browser calls use the console proxy", async () => {
  process.env.NEXT_PUBLIC_ARGUS_API = "http://api.internal:8000";
  const { getConfig, createReview, getRunState, putConfig } = await import("../lib/api");
  const originalFetch = globalThis.fetch;
  const requests: { url: string; headers: Headers }[] = [];
  globalThis.fetch = async (input, init) => {
    requests.push({ url: String(input), headers: new Headers(init?.headers) });
    return Response.json({ models: [], repos: [], id: "1", status: "running" });
  };
  try {
    const config = await getConfig();
    expect(requests[0].url).toBe("http://api.internal:8000/config");
    expect(requests[0].headers.get("authorization")).toBe(`Bearer ${token}`);
    Object.defineProperty(globalThis, "window", { value: {}, configurable: true });
    await getConfig();
    await createReview({ repo: "example/repo", pr: 1 });
    await getRunState("1");
    await putConfig(config);
    expect(requests.slice(1).map(({ url }) => url)).toEqual([
      "/api/argus/config", "/api/argus/reviews", "/api/argus/reviews/1/verdict", "/api/argus/config",
    ]);
    expect(requests.slice(1).every(({ headers }) => !headers.has("authorization"))).toBe(true);
  } finally {
    Reflect.deleteProperty(globalThis, "window");
    globalThis.fetch = originalFetch;
    delete process.env.NEXT_PUBLIC_ARGUS_API;
  }
});
