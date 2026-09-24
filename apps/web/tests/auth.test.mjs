import assert from "node:assert/strict";
import { test } from "node:test";

process.env.NODE_ENV = "test";
process.env.NEXT_PUBLIC_OIDC_ISSUER = "https://id.example/realms/sci";
process.env.NEXT_PUBLIC_OIDC_CLIENT_ID = "scilab-web";

const values = new Map();
globalThis.sessionStorage = {
  getItem: (key) => values.get(key) || null,
  setItem: (key, value) => values.set(key, value),
  removeItem: (key) => values.delete(key),
};
let assigned = "";
globalThis.location = { origin: "http://localhost:3000", pathname: "/runs/new", search: "", assign: (url) => { assigned = url; } };
const { beginLogin, finishLogin, bearerToken } = await import("../lib/auth.ts");

test("OIDC login uses code+PKCE/state and keeps access token in memory", async () => {
  await beginLogin();
  const authorization = new URL(assigned);
  assert.equal(authorization.searchParams.get("response_type"), "code");
  assert.equal(authorization.searchParams.get("code_challenge_method"), "S256");
  assert.ok(authorization.searchParams.get("code_challenge"));
  const state = authorization.searchParams.get("state");
  await assert.rejects(() => finishLogin("code", "wrong-state"), /state/);
  globalThis.fetch = async () => new Response(JSON.stringify({ access_token: "verified-by-api", expires_in: 300 }), { status: 200 });
  assert.equal(await finishLogin("code", state), "/runs/new");
  assert.equal(await bearerToken(), "verified-by-api");
  assert.equal(values.get("scilab.pkce.verifier"), undefined);
  assert.equal(values.get("scilab.pkce.state"), undefined);
});
