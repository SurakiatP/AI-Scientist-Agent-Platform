import type { NextRequest } from "next/server";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(request: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const origin = process.env.SCILAB_API_ORIGIN;
  if (!origin) return new Response("API origin not configured", { status: 503 });
  const { id } = await params;
  const upstream = new URL(`/v1/runs/${encodeURIComponent(id)}/events`, origin);
  upstream.search = request.nextUrl.search;
  const headers = new Headers({ Accept: "text/event-stream", "Accept-Encoding": "identity" });
  const authorization = request.headers.get("Authorization");
  if (authorization) headers.set("Authorization", authorization);
  const response = await fetch(upstream, { headers, cache: "no-store", signal: request.signal });
  if (!response.ok || !response.body) return new Response(await response.text(), { status: response.status });
  return new Response(response.body, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream; charset=utf-8",
      "Cache-Control": "no-cache, no-transform",
      "Content-Encoding": "identity",
      "X-Accel-Buffering": "no",
    },
  });
}
