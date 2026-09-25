import type { NextRequest } from "next/server";
import { proxyRequest } from "../../../proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function GET(request: NextRequest) {
  return proxyRequest(request, { eventStream: true });
}
