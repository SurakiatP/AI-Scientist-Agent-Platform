# AI-Scientist-Agent-Platform

## Wave 15 Web workbench

The Thai-first Web app is in `apps/web`. It uses the Control Plane REST/SSE API; the browser does not enforce tenant permissions or budgets. OIDC access tokens stay in memory and are verified by the Python API. `SCILAB_API_ORIGIN` makes Next.js proxy same-origin `/v1/*` requests to the API.

For an explicitly labelled local demonstration with fixture data (not live integration evidence):

```sh
pnpm --dir apps/web install --frozen-lockfile
pnpm --dir apps/web demo
```

Open `http://127.0.0.1:3000`. The demo starts a loopback-only fixture API on port 18787 and always displays a demo banner. Normal `dev`, `build`, and `start` commands never launch fixture services.

For a real configured environment, set these non-secret values and start the Web separately from the Control Plane:

```sh
SCILAB_API_ORIGIN=https://api.example.org \
NEXT_PUBLIC_OIDC_ISSUER=https://id.example.org/realms/scilab \
NEXT_PUBLIC_OIDC_CLIENT_ID=scilab-web \
pnpm --dir apps/web dev
```

Register `http://127.0.0.1:3000/auth/callback` (or the deployed origin equivalent) as the OIDC public-client redirect URI with authorization-code PKCE. The access token must have the API audience and an active `lab_id` claim; the Python resolver verifies the signature, issuer and audience, then checks the subject against that Lab's membership in PostgreSQL. The Control Plane must be composed with the matching issuer, API audience, JWKS URI, PostgreSQL connection, and `LabAdminService`; those are deployment-specific and are not supplied by the fixture. Live file-object storage, Keycloak, PostgreSQL and Hermes evidence must be checked in staging before claiming end-to-end production readiness.

Local checks:

```sh
pnpm --dir apps/web typecheck
pnpm --dir apps/web build
pnpm --dir apps/web test
pnpm --dir apps/web test:oidc
uv run pytest -q
uv run --project services/lab-operator pytest services/lab-operator/tests -q
```

With `pnpm --dir apps/web demo` running in another terminal, run `SCILAB_DEMO_E2E=1 pnpm --dir apps/web exec playwright test tests/demo.spec.ts` for the synthetic workflow, responsive checks, and axe accessibility scan. `test:oidc` uses a separate local browser fixture; neither test proves a live Keycloak or staging deployment.
