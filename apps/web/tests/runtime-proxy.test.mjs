import assert from "node:assert/strict";
import { afterEach, test } from "node:test";
import { proxyRequest } from "../app/v1/proxy.ts";

const originalOrigin = process.env.SCILAB_API_ORIGIN;
const originalFetch = globalThis.fetch;

afterEach(() => {
  if (originalOrigin === undefined) delete process.env.SCILAB_API_ORIGIN;
  else process.env.SCILAB_API_ORIGIN = originalOrigin;
  globalThis.fetch = originalFetch;
});

function eventRequest(headers = {}) {
  return new Request("https://web.example/v1/runs/run-42/events?from_seq=6", { headers });
}

function openEventStream(headers = {}) {
  const stream = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("data: {\"seq\":7}\n\n"));
    },
  });
  const responseHeaders = new Headers({
    "Content-Type": "text/event-stream; charset=utf-8",
    "Cache-Control": "no-cache",
  });
  for (const cookie of headers.cookies ?? []) responseHeaders.append("Set-Cookie", cookie);
  return new Response(stream, { status: 200, headers: responseHeaders });
}

test("SSE proxy forwards browser auth and cookie headers only to the configured API origin", async () => {
  process.env.SCILAB_API_ORIGIN = "https://api.internal:8443";
  let outgoing;
  globalThis.fetch = async (url, init) => {
    outgoing = { url: new URL(url), headers: new Headers(init.headers) };
    return openEventStream();
  };

  const response = await proxyRequest(eventRequest({
    Authorization: "Bearer access-token",
    Cookie: "session=browser-token",
    "X-Forwarded-Host": "attacker.example",
  }), { eventStream: true });

  assert.equal(outgoing.url.href, "https://api.internal:8443/v1/runs/run-42/events?from_seq=6");
  assert.equal(outgoing.headers.get("authorization"), "Bearer access-token");
  assert.equal(outgoing.headers.get("cookie"), "session=browser-token");
  assert.equal(outgoing.headers.get("x-forwarded-host"), null);
  assert.equal(outgoing.headers.get("accept"), "text/event-stream");
  await response.body.cancel();
});

test("SSE proxy preserves multiple backend cookies and streams event chunks", async () => {
  process.env.SCILAB_API_ORIGIN = "https://api.internal";
  globalThis.fetch = async () => openEventStream({
    cookies: ["session=refreshed; Path=/; HttpOnly", "csrf=next; Path=/; SameSite=Lax"],
  });

  const response = await proxyRequest(eventRequest(), { eventStream: true });

  assert.deepEqual(response.headers.getSetCookie(), [
    "session=refreshed; Path=/; HttpOnly",
    "csrf=next; Path=/; SameSite=Lax",
  ]);
  assert.equal(response.headers.get("cache-control"), "no-cache, no-transform");
  const reader = response.body.getReader();
  const firstChunk = await reader.read();
  assert.equal(new TextDecoder().decode(firstChunk.value), "data: {\"seq\":7}\n\n");
  await reader.cancel();
});

test("SSE proxy returns 503 without a valid runtime origin", async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return openEventStream();
  };

  for (const origin of [undefined, "", "https://api.internal/prefix", "https://user:pass@api.internal"]) {
    if (origin === undefined) delete process.env.SCILAB_API_ORIGIN;
    else process.env.SCILAB_API_ORIGIN = origin;
    const response = await proxyRequest(eventRequest(), { eventStream: true });
    assert.equal(response.status, 503);
  }
  assert.equal(calls, 0);
});
test("runtime proxy returns 502 when the configured API cannot be reached", async () => {
  process.env.SCILAB_API_ORIGIN = "https://api.internal";
  globalThis.fetch = async () => { throw new Error("private backend address"); };
  const response = await proxyRequest(new Request("https://web.example/v1/me"));
  assert.equal(response.status, 502);
  assert.doesNotMatch(await response.text(), /private backend address/);
});

test("runtime origin, methods, request data, and headers are forwarded safely", async () => {
  const requests = [];
  globalThis.fetch = async (url, init) => {
    requests.push({
      url: new URL(url),
      method: init.method,
      headers: new Headers(init.headers),
      body: init.body ? await new Response(init.body).text() : null,
      redirect: init.redirect,
    });
    return new Response("upstream", {
      status: 201,
      headers: {
        Connection: "x-private-response",
        "X-Private-Response": "must-not-cross-hop",
        "Content-Length": "8",
        "Content-Encoding": "identity",
      },
    });
  };

  process.env.SCILAB_API_ORIGIN = "https://api-first.internal/";
  await proxyRequest(new Request("https://web.example/v1/me?detail=1"));
  process.env.SCILAB_API_ORIGIN = "https://api-second.internal";
  const methods = ["HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"];
  for (const method of methods) {
    const hasBody = !["HEAD"].includes(method);
    const request = new Request("https://attacker.example/v1/labs/lab-a/runs?source=web", {
      method,
      headers: {
        Authorization: "Bearer access-token",
        Cookie: "session=browser-token",
        Host: "attacker.example",
        Connection: "x-private",
        "X-Private": "must-not-cross-hop",
        "X-Forwarded-Host": "attacker.example",
        ...(hasBody ? { "Content-Type": "application/json" } : {}),
      },
      ...(hasBody ? { body: "{\"title\":\"run\"}" } : {}),
    });
    const response = await proxyRequest(request);
    if (method === "POST") {
      assert.equal(response.status, 201);
      assert.equal(response.headers.get("connection"), null);
      assert.equal(response.headers.get("x-private-response"), null);
      assert.equal(response.headers.get("content-length"), null);
      assert.equal(response.headers.get("content-encoding"), null);
    }
  }

  assert.equal(requests[0].url.href, "https://api-first.internal/v1/me?detail=1");
  assert.deepEqual(requests.slice(1).map(({ method }) => method), methods);
  for (const outgoing of requests.slice(1)) {
    assert.equal(outgoing.url.href, "https://api-second.internal/v1/labs/lab-a/runs?source=web");
    assert.equal(outgoing.headers.get("authorization"), "Bearer access-token");
    assert.equal(outgoing.headers.get("cookie"), "session=browser-token");
    assert.equal(outgoing.headers.get("host"), null);
    assert.equal(outgoing.headers.get("x-forwarded-host"), null);
    assert.equal(outgoing.headers.get("x-private"), null);
    assert.equal(outgoing.redirect, "manual");
    assert.equal(outgoing.body, methodBody(outgoing.method));
  }
});

function methodBody(method) {
  return method === "HEAD" ? null : "{\"title\":\"run\"}";
}
