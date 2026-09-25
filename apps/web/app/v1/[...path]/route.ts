import { proxyRequest } from "../proxy";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const proxy = (request: Request) => proxyRequest(request);

export { proxy as DELETE, proxy as GET, proxy as HEAD, proxy as OPTIONS, proxy as PATCH, proxy as POST, proxy as PUT };
