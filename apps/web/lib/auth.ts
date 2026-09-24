let accessToken: string | null = null;
let expiresAt = 0;

const issuer = process.env.NEXT_PUBLIC_OIDC_ISSUER?.replace(/\/$/, "");
const clientId = process.env.NEXT_PUBLIC_OIDC_CLIENT_ID;

export const isDemo = process.env.NODE_ENV === "development" && process.env.NEXT_PUBLIC_SCILAB_DEMO === "1";

function randomUrlSafe(bytes = 32) {
  const data = crypto.getRandomValues(new Uint8Array(bytes));
  return btoa(String.fromCharCode(...data)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function challenge(verifier: string) {
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  return btoa(String.fromCharCode(...hash)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export async function beginLogin() {
  if (!issuer || !clientId) throw new Error("ยังไม่ได้กำหนด OIDC issuer/client ID");
  const verifier = randomUrlSafe(48);
  const state = randomUrlSafe();
  sessionStorage.setItem("scilab.pkce.verifier", verifier);
  sessionStorage.setItem("scilab.pkce.state", state);
  sessionStorage.setItem("scilab.pkce.return", location.pathname + location.search);
  const params = new URLSearchParams({
    client_id: clientId, response_type: "code", scope: "openid profile",
    redirect_uri: `${location.origin}/auth/callback`, code_challenge: await challenge(verifier),
    code_challenge_method: "S256", state,
  });
  location.assign(`${issuer}/protocol/openid-connect/auth?${params}`);
}

export async function finishLogin(code: string, state: string) {
  if (!issuer || !clientId) throw new Error("ยังไม่ได้กำหนด OIDC issuer/client ID");
  const verifier = sessionStorage.getItem("scilab.pkce.verifier");
  const expected = sessionStorage.getItem("scilab.pkce.state");
  if (!verifier || !expected || state !== expected) throw new Error("OIDC state ไม่ตรงกัน");
  const response = await fetch(`${issuer}/protocol/openid-connect/token`, {
    method: "POST", headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "authorization_code", client_id: clientId, code, code_verifier: verifier, redirect_uri: `${location.origin}/auth/callback` }),
  });
  if (!response.ok) throw new Error(`เข้าสู่ระบบไม่สำเร็จ (${response.status})`);
  const result = await response.json();
  if (typeof result.access_token !== "string" || !result.access_token) throw new Error("OIDC ไม่ส่ง access token");
  accessToken = result.access_token;
  expiresAt = Date.now() + Math.max(0, Number(result.expires_in || 0) - 30) * 1000;
  const destination = sessionStorage.getItem("scilab.pkce.return") || "/runs/new";
  sessionStorage.removeItem("scilab.pkce.verifier");
  sessionStorage.removeItem("scilab.pkce.state");
  sessionStorage.removeItem("scilab.pkce.return");
  return destination.startsWith("/") && !destination.startsWith("//") ? destination : "/runs/new";
}

export async function bearerToken() {
  if (isDemo) return "demo-only";
  if (!issuer || !clientId) {
    if (process.env.NODE_ENV === "development") return null; // contract-mock tests still get server-side 401
    throw new Error("ยังไม่ได้กำหนด OIDC issuer/client ID");
  }
  if (accessToken && Date.now() < expiresAt) return accessToken;
  await beginLogin();
  throw new Error("กำลังนำไปหน้าเข้าสู่ระบบ");
}
