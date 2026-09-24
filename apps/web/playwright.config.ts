import { defineConfig, devices } from "@playwright/test";

const oidcFixture = process.env.SCILAB_OIDC_E2E === "1";
const baseURL = `http://127.0.0.1:${oidcFixture ? 3105 : 3000}`;

export default defineConfig({
  testDir: "./tests",
  testMatch: oidcFixture ? "auth-flow.spec.ts" : undefined,
  testIgnore: oidcFixture ? [] : ["auth-flow.spec.ts"],
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: {
    command: oidcFixture ? "pnpm dev --port 3105" : "pnpm dev",
    url: baseURL,
    reuseExistingServer: !oidcFixture && !process.env.CI,
    ...(oidcFixture ? { env: { NEXT_PUBLIC_OIDC_ISSUER: "https://id.example/realms/sci", NEXT_PUBLIC_OIDC_CLIENT_ID: "scilab-web", NEXT_PUBLIC_SCILAB_DEMO: "0" } } : {}),
    timeout: 120_000,
  },
});
