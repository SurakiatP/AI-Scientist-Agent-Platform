import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";

test("OIDC authorization-code PKCE returns to the authenticated research workflow", async ({ page }) => {
  const issuer = "https://id.example/realms/sci";
  const callback = "http://127.0.0.1:3105/auth/callback";
  const token = "local-oidc-fixture-token";
  let authorization: URL | undefined;
  let exchanges = 0;
  let authenticatedReads = 0;

  await page.route(`${issuer}/protocol/openid-connect/auth?*`, async (route) => {
    authorization = new URL(route.request().url());
    expect(authorization.searchParams.get("client_id")).toBe("scilab-web");
    expect(authorization.searchParams.get("response_type")).toBe("code");
    expect(authorization.searchParams.get("code_challenge_method")).toBe("S256");
    expect(authorization.searchParams.get("redirect_uri")).toBe(callback);
    await route.fulfill({ status: 302, headers: { location: `${callback}?code=local-code&state=${authorization.searchParams.get("state")}` }, body: "" });
  });
  await page.route(`${issuer}/protocol/openid-connect/token`, async (route) => {
    if (route.request().method() === "OPTIONS") {
      await route.fulfill({ status: 204, headers: { "access-control-allow-origin": "http://127.0.0.1:3105", "access-control-allow-methods": "POST, OPTIONS", "access-control-allow-headers": "content-type" } });
      return;
    }
    exchanges++;
    const body = new URLSearchParams(route.request().postData() || "");
    expect(body.get("grant_type")).toBe("authorization_code");
    expect(body.get("client_id")).toBe("scilab-web");
    expect(body.get("code")).toBe("local-code");
    expect(body.get("redirect_uri")).toBe(callback);
    const verifier = body.get("code_verifier") || "";
    expect(verifier.length).toBeGreaterThan(40);
    expect(createHash("sha256").update(verifier).digest("base64url")).toBe(authorization?.searchParams.get("code_challenge"));
    await route.fulfill({ json: { access_token: token, token_type: "Bearer", expires_in: 300 }, headers: { "access-control-allow-origin": "http://127.0.0.1:3105" } });
  });
  await page.route("**/v1/me", async (route) => {
    if (route.request().headers().authorization !== `Bearer ${token}`) {
      await route.fulfill({ status: 401, json: { detail: "unauthorized" } });
      return;
    }
    authenticatedReads++;
    await route.fulfill({ json: { principal: "user:fixture", lab_id: "lab-a", scopes: ["runs:write"] } });
  });
  await page.route("**/v1/labs/lab-a/skills", (route) => route.fulfill({ json: [{ name: "general-research", approved: true }] }));

  await page.goto("/runs/new");
  await expect(page).toHaveURL("http://127.0.0.1:3105/runs/new");
  await expect(page.getByRole("button", { name: "เริ่ม Run" })).toBeVisible();
  expect(exchanges).toBe(1);
  expect(authenticatedReads).toBeGreaterThan(0);
  expect(await page.evaluate(() => ({ local: { ...localStorage }, session: { ...sessionStorage } }))).toEqual({ local: {}, session: {} });
});
