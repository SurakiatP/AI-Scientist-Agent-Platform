const hopByHopHeaders = [
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
];

const forwardedHeaders = [
  "forwarded",
  "x-forwarded-for",
  "x-forwarded-host",
  "x-forwarded-port",
  "x-forwarded-proto",
  "x-real-ip",
];

function stripHopByHop(headers: Headers) {
  for (const name of headers.get("connection")?.split(",") ?? []) {
    const token = name.trim().toLowerCase();
    if (/^[!#$%&'*+.^_`|~0-9a-z-]+$/.test(token)) headers.delete(token);
  }
  for (const name of hopByHopHeaders) headers.delete(name);
}

function configuredOrigin() {
  const value = process.env.SCILAB_API_ORIGIN;
  if (!value) return null;
  try {
    const origin = new URL(value);
    if (
      !["http:", "https:"].includes(origin.protocol) ||
      origin.username ||
      origin.password ||
      origin.pathname !== "/" ||
      origin.search ||
      origin.hash
    ) return null;
    return origin;
  } catch {
    return null;
  }
}

function responseHeaders(source: Headers, eventStream: boolean) {
  const headers = new Headers(source);
  stripHopByHop(headers);
  headers.delete("content-length");
  headers.delete("content-encoding");

  const cookies = (source as Headers & { getSetCookie?: () => string[] }).getSetCookie?.() ?? [];
  if (cookies.length) {
    headers.delete("set-cookie");
    for (const cookie of cookies) headers.append("set-cookie", cookie);
  }

  if (eventStream) {
    headers.set("content-type", source.get("content-type") ?? "text/event-stream; charset=utf-8");
    headers.set("cache-control", "no-cache, no-transform");
    headers.set("content-encoding", "identity");
    headers.set("x-accel-buffering", "no");
  }
  return headers;
}

export async function proxyRequest(request: Request, options: { eventStream?: boolean } = {}) {
  const origin = configuredOrigin();
  if (!origin) return new Response("API origin not configured", { status: 503 });

  const target = new URL(request.url);
  target.protocol = origin.protocol;
  target.host = origin.host;
  target.username = "";
  target.password = "";

  const headers = new Headers(request.headers);
  stripHopByHop(headers);
  headers.delete("host");
  for (const name of forwardedHeaders) headers.delete(name);
  if (options.eventStream) {
    headers.set("accept", "text/event-stream");
    headers.set("accept-encoding", "identity");
  }

  const init: RequestInit & { duplex?: "half" } = {
    method: request.method,
    headers,
    cache: "no-store",
    redirect: "manual",
    signal: request.signal,
  };
  if (request.method !== "GET" && request.method !== "HEAD" && request.body) {
    init.body = request.body;
    init.duplex = "half";
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, init);
  } catch {
    return new Response("API unavailable", { status: 502 });
  }
  const isEventStream = Boolean(options.eventStream && upstream.ok && upstream.body);
  const body = request.method === "HEAD" || [204, 205, 304].includes(upstream.status) ? null : upstream.body;
  return new Response(body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders(upstream.headers, isEventStream),
  });
}
