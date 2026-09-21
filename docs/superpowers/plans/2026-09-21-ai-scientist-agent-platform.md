# AI Scientist Agent Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `delegate-build` phase-by-phase. A planner validates ordered waves, workers receive non-overlapping path scopes, and an independent reviewer must return `PASS` before the next phase. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the TOR-compliant AI Scientist platform from authenticated research submission through isolated execution, review, provenance, and multi-protocol result delivery.

**Architecture:** Use a modular Python FastAPI control plane, separate Python Kopf Lab Operator, and Next.js web client. One Hermes PI serves each Lab. OpenSandbox on Kubernetes creates ephemeral Kata sandboxes on demand for code-capable delegated agents. Postgres stores control data, MinIO/S3 stores artifacts, NATS carries normalized events, and Keycloak supplies identity.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, psycopg, NATS, Kopf, OpenSandbox release-1.1.0, Kata Containers, Keycloak, OPA, Postgres/pgvector, MinIO/S3, LiteLLM, OpenTelemetry, Langfuse, Prometheus/Grafana, Loki, Next.js App Router, TanStack Query, pytest, Playwright

**Spec:** `docs/specs/ai-scientist-agent-platform-spec.md`

## Global Constraints

- Pin Scientific Agent Skills to `v2.69.0`; install only approved general-research packs.
- Use Python 3.13 for platform backend and skill tooling.
- Use one Hermes PI instance per Lab and Hermes Runs API for research.
- Map REST, A2A, and MCP to one Run resource and normalized identity.
- Keep Hermes ports 8642 and 9900 ClusterIP-only.
- Use OpenSandbox `release-1.1.0` with Kata RuntimeClass, default-deny egress, and Credential Vault.
- Keep sensitive workloads inside Kubernetes; Modal accepts non-sensitive workloads only.
- Use Keycloak OIDC/OAuth 2.1 and roles owner, researcher, viewer.
- Keep Temporal, data residency, cron literature watch, Daytona, and Nextflow out of initial delivery.
- Do not claim external compliance certification or SLA absent from TOR.

## Effort and Ownership

Effort is an engineering estimate, not a TOR fact. `S` = 2-4 person-days, `M` = 5-9, `L` = 10-15. Owners are roles until named staff are assigned.

## Planned File Structure

Detailed ownership, dependency, and delegation boundaries: `docs/architecture/project-structure.md`.

```text
apps/web/                         Next.js user interface
services/control-plane/           FastAPI modular control plane
services/lab-operator/            Kopf Lab and Hermes reconciler
contracts/                        JSON Schema, OpenAPI, event and manifest contracts
sdk/python/                       Python REST/SSE example SDK
skills/platform/                  Four platform skills
skills/packs/                     Pinned upstream pack manifest and evaluation
deploy/helm/scilab/               Platform deployment
deploy/policies/                  OPA, NetworkPolicy, sandbox egress
tests/acceptance/                 Cross-system TOR acceptance proof
docs/                             Architecture, API, user, admin, runbook
```

Only Web, Control Plane, and Lab Operator are deployable runtimes initially.
Do not split control-plane modules into additional services without measured
scaling, security, or ownership evidence.

## Delegate-build Execution Contract

- Planner/final reviewer: `gpt-5.6-sol`, high effort.
- Implementation workers: `gpt-5.6-luna`, xhigh effort.
- Workers may edit only task-owned paths; concurrent scopes must not overlap.
- One worker per wave owns root dependency files, lockfiles, generated contracts,
  and shared Helm values.
- The main agent inspects the aggregate diff and runs focused verification after
  every wave.
- Do not delegate secrets, `.env` files, credentials, or production data.
- A phase advances only when its independent reviewer returns `PASS` with test
  evidence. A contract-changing gap returns to the owner for a decision.

### Phase and Wave Schedule

| Phase | Ordered waves | Parallel worker scopes |
|---|---|---|
| 1. Control Plane Foundation | `1` -> `2` -> `3` -> `4` | None; each establishes the next contract |
| 2. Lab Runtime and Skills | `5` -> `6` -> `7` -> `8` | None; runtime chain is sequential |
| 3. Evidence and Governance | `9 + 10 + 11` | Provenance, approvals, and telemetry own separate modules/tests |
| 4. Public Interfaces | `12` -> `13 + 14` -> `15` | A2A and MCP own separate adapters/tests |
| 5. Delivery and Proof | `16` -> `17` -> `18` | None; deployment precedes proof and handoff |

Task 15 has an additional Hallmark gate: pre-flight scan; ask Audience, Use
case, and Tone; state exact frontend files before editing; use token-only colors
and fonts; implement eight interaction states; verify 320/375/414/768 px; pass
the Hallmark slop test. Hallmark does not run during backend phases.

---

## Workstream 1 Control Plane Foundation

### Deliverable 1.1 Executable contracts

#### Task 1 Define shared contracts and Python workspace

**Owner:** Backend Lead  
**Effort:** M  
**Dependencies:** None  
**Requirements:** FR-009, FR-012, FR-029, FR-030, NFR-009, CON-002, CON-003

**Files:**
- Create: `pyproject.toml`
- Create: `services/control-plane/src/scilab/contracts.py`
- Create: `contracts/events/run-event.schema.json`
- Create: `contracts/provenance/run-manifest.schema.json`
- Create: `contracts/delegation/research-result.schema.json`
- Create: `tests/contracts/test_contracts.py`

**Interfaces:**
- Produces: `RunEvent`, `RunManifest`, `ResearchResult` Pydantic models matching JSON Schema.
- Consumes: exact fields from TOR A.4.2, A.4.3, A.5.1.

**Acceptance:** Valid examples pass; missing required fields, invalid confidence, and unknown event types fail.

**Evidence:** `pytest tests/contracts/test_contracts.py -q` output plus generated schema diff.

- [ ] Write failing tests that load all three schemas and reject `confidence=1.1` and missing `lab_id`.

```python
def test_claim_confidence_is_bounded():
    with pytest.raises(ValidationError):
        ResearchResult(claims=[{"text": "x", "evidence": ["art_1"], "confidence": 1.1}], artifacts=[], caveats=[])
```

- [ ] Run `uv run pytest tests/contracts/test_contracts.py -q`; expect failure because models do not exist.
- [ ] Add minimal Pydantic models and checked-in JSON Schemas with identical field names.
- [ ] Run the test again; expect all contract tests to pass.
- [ ] Commit: `feat: define platform contracts`

### Deliverable 1.2 Tenant identity and persistence

#### Task 2 Implement normalized identity and tenant authorization

**Owner:** Security Backend Engineer  
**Effort:** M  
**Dependencies:** Task 1  
**Requirements:** FR-006, FR-007, FR-008, NFR-001, NFR-003, NFR-004, CR-002, CR-003, CR-009, CON-008

**Files:**
- Create: `services/control-plane/src/scilab/identity.py`
- Create: `services/control-plane/src/scilab/tenancy.py`
- Create: `services/control-plane/src/scilab/db.py`
- Create: `services/control-plane/migrations/001_identity_and_labs.sql`
- Create: `services/control-plane/tests/test_identity.py`
- Create: `services/control-plane/tests/test_tenant_isolation.py`

**Interfaces:**
- Produces: `Identity(lab_id: str, principal: str, scopes: frozenset[str])`.
- Produces: `require_scope(identity, scope)` and Lab-scoped repository methods that always require `lab_id`.
- Consumes: verified Keycloak claims, Lab API-key hashes, registered A2A peers.

**Acceptance:** Web, REST, MCP, and A2A identities normalize to one shape; cross-Lab access fails before database mutation.

**Evidence:** identity matrix test and two-Lab negative test.

- [ ] Write tests for OIDC, Lab key, MCP token, A2A peer, missing scope, and cross-Lab artifact access.

```python
def test_cross_lab_access_is_denied(identity_lab_a, run_lab_b):
    with pytest.raises(TenantAccessDenied):
        load_run(identity_lab_a, run_lab_b.id)
```

- [ ] Run `uv run pytest services/control-plane/tests/test_identity.py services/control-plane/tests/test_tenant_isolation.py -q`; expect failure.
- [ ] Implement normalized identity, hashed API-key lookup, scoped repositories, and database constraints.
- [ ] Re-run tests; expect pass with no plaintext API key in database fixtures.
- [ ] Commit: `feat: enforce tenant identity`

### Deliverable 1.3 Run lifecycle

#### Task 3 Implement Run state machine, timeout, retry, and idempotency

**Owner:** Backend Engineer  
**Effort:** L  
**Dependencies:** Tasks 1-2  
**Requirements:** FR-009, FR-025, NFR-007, NFR-011, CON-002

**Files:**
- Create: `services/control-plane/src/scilab/runs/model.py`
- Create: `services/control-plane/src/scilab/runs/state.py`
- Create: `services/control-plane/src/scilab/runs/service.py`
- Create: `services/control-plane/migrations/002_runs.sql`
- Create: `services/control-plane/tests/runs/test_state.py`
- Create: `services/control-plane/tests/runs/test_idempotency.py`

**Interfaces:**
- Produces: `RunService.create`, `transition`, `stop`, `retry`, `get`, `list`.
- State values: `queued`, `running`, `awaiting_approval`, `completed`, `failed`, `cancelled`.
- Timeout values: queue 30 minutes, run default 120 minutes, approval 24 hours, heartbeat 90 seconds, retries two.

**Acceptance:** Only TOR transitions succeed; external duplicate keys return the existing Run; internal retry requeues the same Run without creating a duplicate.

**Evidence:** deterministic-clock transition matrix and idempotency tests.

- [ ] Write transition-table and duplicate-key tests with a fake clock.

```python
@pytest.mark.parametrize("source,target", [("queued", "running"), ("running", "awaiting_approval"), ("running", "completed")])
def test_allowed_transitions(source, target):
    assert transition(source, target).state == target
```

- [ ] Run focused tests; expect missing implementation failures.
- [ ] Implement the smallest explicit transition table and transactional idempotency record.
- [ ] Run focused tests; expect pass.
- [ ] Commit: `feat: add run lifecycle`

### Deliverable 1.4 Normalized event stream

#### Task 4 Implement NATS events and resumable SSE

**Owner:** Backend Engineer  
**Effort:** M  
**Dependencies:** Tasks 1-3  
**Requirements:** FR-002, FR-003, FR-009, FR-013, FR-023, FR-029, NFR-008, NFR-012

**Files:**
- Create: `services/control-plane/src/scilab/events.py`
- Create: `services/control-plane/src/scilab/sse.py`
- Create: `services/control-plane/tests/test_events.py`
- Create: `services/control-plane/tests/test_sse.py`

**Interfaces:**
- Produces: `publish_event(run_id, type, payload, source)` with monotonic ULID and `seq`.
- Produces: `stream_events(run_id, from_seq)` with 15-second heartbeat.

**Acceptance:** reconnect from `seq` loses no event; event payload validates; secrets in tool arguments are redacted.

**Evidence:** event replay test, schema validation log, redaction fixture.

- [ ] Write tests for ordering, resume, heartbeat, and redaction.
- [ ] Run `uv run pytest services/control-plane/tests/test_events.py services/control-plane/tests/test_sse.py -q`; expect failure.
- [ ] Implement Postgres event persistence plus NATS fan-out and SSE replay.
- [ ] Re-run focused tests; expect pass.
- [ ] Commit: `feat: stream normalized run events`

## Workstream 2 Lab Runtime and Skills

### Deliverable 2.1 Lab and Hermes provisioning

#### Task 5 Build Kopf Lab Operator

**Owner:** Platform Engineer  
**Effort:** L  
**Dependencies:** Task 2  
**Requirements:** FR-010, FR-014, NFR-001, NFR-002, CR-004, DEL-002, DEL-003, CON-001, CON-008

**Files:**
- Create: `services/lab-operator/pyproject.toml`
- Create: `services/lab-operator/src/lab_operator/handlers.py`
- Create: `services/lab-operator/src/lab_operator/resources.py`
- Create: `deploy/helm/scilab/crds/lab.yaml`
- Create: `services/lab-operator/tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Lab` CR with Lab ID, pinned Hermes digest, skill image digest, resource limits.
- Produces: one Hermes Deployment, PVC, Secret references, ClusterIP Services, and NetworkPolicy per Lab.

**Acceptance:** reconcile is idempotent; ports 8642/9900 have no Ingress/LoadBalancer; only Gateway, Run Service, and registered Hermes peers can connect.

**Evidence:** rendered Kubernetes objects, reconcile test, in-cluster denied/allowed network probes.

- [ ] Write a failing reconcile test asserting exact resource names and ClusterIP-only services.
- [ ] Run `uv run pytest services/lab-operator/tests/test_reconcile.py -q`; expect failure.
- [ ] Implement minimal Kopf handlers and resource builders.
- [ ] Run tests and render Helm manifests; expect pass.
- [ ] Commit: `feat: provision isolated labs`

### Deliverable 2.2 Agent sandbox execution

#### Task 6 Integrate OpenSandbox with Kata and Credential Vault

**Owner:** Platform Security Engineer  
**Effort:** L  
**Dependencies:** Tasks 2, 5  
**Requirements:** FR-018, FR-019, NFR-001, NFR-002, CR-003, CR-009, DEL-005

**Files:**
- Create: `services/control-plane/src/scilab/sandbox.py`
- Create: `deploy/helm/scilab/templates/opensandbox.yaml`
- Create: `deploy/policies/sandbox-egress.yaml`
- Create: `services/control-plane/tests/test_sandbox.py`
- Create: `tests/acceptance/test_sandbox_security.py`

**Interfaces:**
- Produces: `SandboxBroker.create_for_agent`, `run`, `upload`, `download`, `destroy`.
- Sandbox config: OpenSandbox release-1.1.0, Kata RuntimeClass, expiry at Run timeout, default-deny egress, explicit FQDN allowlist, Credential Vault.

**Acceptance:** code agent gets an ephemeral sandbox; non-code agent gets none; direct secret read and denied egress fail; artifact export succeeds; sandbox is destroyed at terminal Run state.

**Evidence:** OpenSandbox lifecycle log, egress denial test, secret-redaction test, cleanup proof.

- [ ] Write failing fake-SDK tests for on-demand creation and guaranteed cleanup.

```python
async def test_non_code_role_does_not_allocate_sandbox(broker):
    assert await broker.for_role("writer", needs_tools=False) is None
```

- [ ] Run focused unit and acceptance tests; expect failure.
- [ ] Implement thin OpenSandbox Python SDK adapter and Kata deployment values.
- [ ] Re-run tests in staging Kubernetes; expect pass.
- [ ] Commit: `feat: isolate agent execution`

### Deliverable 2.3 Controlled skill supply chain

#### Task 7 Build selected skill packs and four platform skills

**Owner:** AI Platform Engineer  
**Effort:** L  
**Dependencies:** Tasks 1, 6  
**Requirements:** FR-016, FR-017, NFR-005, NFR-006, CR-005, CR-006, DEL-004, CON-006, CON-011

**Files:**
- Create: `skills/packs/general-research.yaml`
- Create: `skills/platform/sci-run-protocol/SKILL.md`
- Create: `skills/platform/artifact-store/SKILL.md`
- Create: `skills/platform/provenance-manifest/SKILL.md`
- Create: `skills/platform/approval-etiquette/SKILL.md`
- Create: `scripts/build_skill_image.py`
- Create: `tests/skills/test_supply_chain.py`

**Interfaces:**
- Consumes: upstream tag `v2.69.0` and explicit skill names in `general-research.yaml`.
- Produces: image digest, source commit, scan report, license inventory, evaluation report.

**Acceptance:** unpinned source, missing license, failed scan, or failed evaluation blocks build; runtime mount is read-only.

**Evidence:** signed pack manifest, scanner output, license inventory, eval results, mount test.

- [ ] Write tests that reject a moving Git ref and a pack containing an unapproved skill.
- [ ] Run `uv run pytest tests/skills/test_supply_chain.py -q`; expect failure.
- [ ] Implement one build script using Python standard library subprocess plus existing scanner/installer CLIs.
- [ ] Build and inspect the general-research image; expect all gates and read-only mount test to pass.
- [ ] Commit: `feat: add governed skill packs`

## Workstream 3 Research Orchestration and Evidence

### Deliverable 3.1 Hermes orchestration

#### Task 8 Implement Hermes Runs API adapter and research cycle

**Owner:** AI Backend Engineer  
**Effort:** L  
**Dependencies:** Tasks 1, 3-7  
**Requirements:** FR-014, FR-015, FR-025, FR-026, FR-030, NFR-010, NFR-011, DEL-003, CON-001, CON-002

**Files:**
- Create: `services/control-plane/src/scilab/hermes.py`
- Create: `services/control-plane/src/scilab/orchestration.py`
- Create: `services/control-plane/tests/test_hermes.py`
- Create: `services/control-plane/tests/test_research_cycle.py`

**Interfaces:**
- Produces: `HermesClient.start_run`, `events`, `stop`, `ask_lab`.
- Produces: `ResearchCycle.plan`, `gather`, `analyze`, `critique`, `report`.
- Delegation roles and model aliases match TOR A.5.1; every delegation supplies `ResearchResult` schema.

**Acceptance:** PI follows five stages, Reviewer provider differs from PI, critique stops after two rounds, and schema repair is attempted once.

**Evidence:** recorded fake-Hermes protocol test and one staging Run trace.

- [ ] Write failing tests for Runs API usage, role/model selection, schema repair, and two-round critique ceiling.
- [ ] Run focused tests; expect failure.
- [ ] Implement minimal Hermes HTTP adapter and explicit five-stage coordinator.
- [ ] Run tests and one staging trace; expect pass.
- [ ] Commit: `feat: orchestrate research runs`

### Deliverable 3.2 Artifact and provenance service

#### Task 9 Implement artifact registry and sealed manifest

**Owner:** Backend Engineer  
**Effort:** M  
**Dependencies:** Tasks 1-4, 8  
**Requirements:** FR-004, FR-005, FR-012, FR-025, NFR-008, NFR-009, CR-008, DEL-002

**Files:**
- Create: `services/control-plane/src/scilab/artifacts.py`
- Create: `services/control-plane/src/scilab/provenance.py`
- Create: `services/control-plane/migrations/003_artifacts.sql`
- Create: `services/control-plane/tests/test_provenance.py`

**Interfaces:**
- Produces: `ArtifactService.register`, `presign`, `list_for_run`.
- Produces: `ManifestService.seal(run_id)` returning immutable manifest artifact and SHA-256.

**Acceptance:** every claim has evidence; every artifact has hash/size/producer; presigned URL lasts 15 minutes; post-seal mutation fails verification.

**Evidence:** manifest JSON, hash verification output, tamper-negative test.

- [ ] Write failing tests for missing claim evidence, URL expiry, and sealed-manifest tampering.
- [ ] Run `uv run pytest services/control-plane/tests/test_provenance.py -q`; expect failure.
- [ ] Implement S3/MinIO registration and deterministic manifest serialization.
- [ ] Re-run tests; expect pass.
- [ ] Commit: `feat: seal research provenance`

### Deliverable 3.3 Approval and policy

#### Task 10 Implement OPA-backed approval gates

**Owner:** Security Backend Engineer  
**Effort:** M  
**Dependencies:** Tasks 2-4, 8  
**Requirements:** FR-003, FR-011, NFR-007, CR-009, DEL-002

**Files:**
- Create: `services/control-plane/src/scilab/approvals.py`
- Create: `deploy/policies/approval.rego`
- Create: `services/control-plane/migrations/004_approvals.sql`
- Create: `services/control-plane/tests/test_approvals.py`

**Interfaces:**
- Produces: `evaluate_action`, `request_approval`, `decide_approval`, `expire_approvals`.
- Approval event fields match TOR A.4.2.

**Acceptance:** blocked action cannot execute before approval; reject/expiry cancels Run; decision requires `runs:approve`; record includes actor/note/policy rule.

**Evidence:** OPA decision log and approve/reject/expiry tests.

- [ ] Write failing tests for blocked execution, missing scope, approval, rejection, and 24-hour expiry.
- [ ] Run focused tests; expect failure.
- [ ] Implement one OPA client and transactional approval record.
- [ ] Re-run tests; expect pass.
- [ ] Commit: `feat: enforce approval policy`

### Deliverable 3.4 Metering and observability

#### Task 11 Correlate model usage, compute cost, audit, traces, metrics, and logs

**Owner:** SRE Engineer  
**Effort:** L  
**Dependencies:** Tasks 2-4, 6, 8  
**Requirements:** FR-013, FR-023, FR-024, CR-007, DEL-009, CON-007

**Files:**
- Create: `services/control-plane/src/scilab/metering.py`
- Create: `services/control-plane/src/scilab/telemetry.py`
- Create: `services/control-plane/src/scilab/audit.py`
- Create: `services/control-plane/tests/test_metering.py`
- Create: `deploy/helm/scilab/templates/observability.yaml`

**Interfaces:**
- Produces: `record_usage(run_id, actor, source, tokens, compute, cost_thb)` and append-only audit events.
- Propagates `run_id` and `lab_id` through OTel baggage.

**Acceptance:** one Run correlates Langfuse trace, Prometheus metrics, Loki logs, LiteLLM usage, compute usage, and audit without secret/tool-argument leakage.

**Evidence:** correlation query screenshots/JSON and budget-exceeded cancellation test.

- [ ] Write failing tests for aggregation, Lab budget stop, source dimension, and redaction.
- [ ] Run focused tests; expect failure.
- [ ] Implement minimal metering/audit records and OTel instrumentation.
- [ ] Execute one staging Run and save correlation evidence.
- [ ] Commit: `feat: meter and trace runs`

## Workstream 4 Public Interfaces and Web

### Deliverable 4.1 REST OpenAPI and Python SDK

#### Task 12 Implement the TOR REST surface

**Owner:** API Engineer  
**Effort:** L  
**Dependencies:** Tasks 2-11  
**Requirements:** FR-008, FR-020, FR-027, NFR-003, NFR-008, DEL-006, CON-003

**Files:**
- Create: `services/control-plane/src/scilab/api/rest.py`
- Create: `contracts/openapi/scilab.yaml`
- Create: `sdk/python/scilab_client.py`
- Create: `services/control-plane/tests/api/test_rest_contract.py`

**Interfaces:**
- Implements all 14 method/path groups in TOR A.6.2 with the exact special behavior listed there.

**Acceptance:** OpenAPI 3.1 validates; endpoint contract tests pass; SDK demonstrates create, follow SSE, approve, stop, and fetch artifacts.

**Evidence:** OpenAPI validation output, contract test report, executable SDK transcript.

- [ ] Write one parameterized failing contract test covering every TOR method/path row.
- [ ] Run `uv run pytest services/control-plane/tests/api/test_rest_contract.py -q`; expect failure.
- [ ] Implement thin FastAPI routes that call existing services; do not duplicate business logic.
- [ ] Validate OpenAPI and run the SDK example; expect pass.
- [ ] Commit: `feat: expose REST run API`

### Deliverable 4.2 A2A façade

#### Task 13 Implement A2A v1.0 and Lab Agent Cards

**Owner:** API Engineer  
**Effort:** M  
**Dependencies:** Tasks 2-4, 8-12  
**Requirements:** FR-008, FR-021, FR-028, NFR-003, NFR-004, DEL-007, CON-003, CON-005

**Files:**
- Create: `services/control-plane/src/scilab/api/a2a.py`
- Create: `services/control-plane/tests/api/test_a2a.py`
- Create: `contracts/a2a/agent-card.schema.json`

**Interfaces:**
- Implements Agent Card, SendMessage, SendStreamingMessage, GetTask, ListTasks, CancelTask, SubscribeToTask.
- Maps `contextId` to Hermes session key and TOR task states to Run states.

**Acceptance:** registered peer bearer token over TLS works; unknown peer fails; push body has valid HMAC-SHA256; streaming and state mapping match TOR.

**Evidence:** A2A conformance transcript and signed webhook fixture.

- [ ] Write failing tests for Agent Card, peer auth, state map, stream, cancel, and HMAC.
- [ ] Run focused tests; expect failure.
- [ ] Implement a2a-sdk handlers as adapters over Run Service.
- [ ] Re-run tests; expect pass.
- [ ] Commit: `feat: expose A2A run facade`

### Deliverable 4.3 MCP façade

#### Task 14 Implement FastMCP Streamable HTTP server

**Owner:** API Engineer  
**Effort:** M  
**Dependencies:** Tasks 2-4, 8-12  
**Requirements:** FR-008, FR-022, NFR-003, NFR-004, DEL-008, CON-003, CON-004

**Files:**
- Create: `services/control-plane/src/scilab/api/mcp.py`
- Create: `services/control-plane/tests/api/test_mcp.py`

**Interfaces:**
- Tools: `start_research`, `get_run`, `wait_run`, `ask_lab`, `list_skills`, `get_artifact`, `approve`.
- Resources: `scilab://runs/{id}/report`, `scilab://runs/{id}/manifest`.
- Prompt: `research_brief`.

**Acceptance:** OAuth audience is `mcp.scilab`; `wait_run` caps at 1,800 seconds and emits progress; approval requires `runs:approve`; Lab comes only from token.

**Evidence:** MCP inspector transcript and scope-negative tests.

- [ ] Write failing tests for all tools/resources/prompt and token-derived Lab enforcement.
- [ ] Run focused tests; expect failure.
- [ ] Implement FastMCP adapters over existing services.
- [ ] Run tests and MCP inspector; expect pass.
- [ ] Commit: `feat: expose MCP research tools`

### Deliverable 4.4 Web application

#### Task 15 Build submit, tracking, result, and Lab administration screens

**Owner:** Frontend Engineer  
**Effort:** L  
**Dependencies:** Tasks 12, 14  
**Requirements:** FR-001, FR-002, FR-003, FR-004, FR-005, FR-006, FR-007, DEL-001

**Files:**
- Create: `apps/web/app/runs/new/page.tsx`
- Create: `apps/web/app/runs/[id]/page.tsx`
- Create: `apps/web/app/labs/[id]/page.tsx`
- Create: `apps/web/lib/api.ts`
- Create: `apps/web/tests/research-flow.spec.ts`

**Interfaces:**
- Uses REST/SSE only; no business logic in Web.
- Displays goal, inputs, packs, budget, state, steps, tool calls, cost, approvals, report, artifacts, provenance, citations, members, keys, peers, budget, usage.

**Acceptance:** Playwright user completes submit, live track, approve, result download, and Lab admin; viewer cannot mutate.

**Evidence:** Playwright report and accessibility scan for primary pages.

- [ ] Write failing Playwright flow using mocked OpenAPI responses and SSE events.
- [ ] Run `pnpm --dir apps/web test:e2e`; expect failure because pages do not exist.
- [ ] Implement accessible Next.js pages with TanStack Query and native controls.
- [ ] Run tests against staging API; expect pass.
- [ ] Commit: `feat: add research web workflow`

## Workstream 5 Deployment, Acceptance, and Handoff

### Deliverable 5.1 Secure deployment

#### Task 16 Package staging and production Helm deployment

**Owner:** Platform Engineer  
**Effort:** L  
**Dependencies:** Tasks 5-15  
**Requirements:** NFR-001, NFR-002, NFR-004, CR-001, CR-004, CR-009, DEL-002, DEL-005, DEL-009, CON-008, CON-010

**Files:**
- Create: `deploy/helm/scilab/Chart.yaml`
- Create: `deploy/helm/scilab/values.yaml`
- Create: `deploy/helm/scilab/values-staging.yaml`
- Create: `deploy/helm/scilab/values-production.yaml`
- Create: `tests/acceptance/test_network_boundaries.py`

**Interfaces:**
- Deploys control plane, Lab Operator, OpenSandbox, Keycloak integration, OPA, Postgres, MinIO, NATS, LiteLLM, and telemetry endpoints.

**Acceptance:** Helm lint/template passes; staging install succeeds; only public Gateway/Web endpoints expose ingress; sandbox uses Kata; network probes match policy.

**Evidence:** rendered manifests, image digests/signature checks, Helm test output, network probe report.

- [ ] Write manifest assertions for ClusterIP-only Hermes, Kata RuntimeClass, digest pins, and default-deny policies.
- [ ] Run assertions against empty chart; expect failure.
- [ ] Add minimal chart and environment values without cloud-specific resources.
- [ ] Install to staging and run assertions; expect pass.
- [ ] Commit: `feat: package secure deployment`

### Deliverable 5.2 TOR acceptance proof

#### Task 17 Run end-to-end and compliance acceptance suite

**Owner:** QA Lead  
**Effort:** L  
**Dependencies:** Tasks 1-16  
**Requirements:** FR-001-FR-030, NFR-001-NFR-012, CR-001-CR-009, DEL-001-DEL-009, CON-001-CON-011

**Files:**
- Create: `tests/acceptance/test_research_workflow.py`
- Create: `tests/acceptance/test_protocol_equivalence.py`
- Create: `tests/acceptance/test_tenant_isolation.py`
- Create: `tests/acceptance/test_provenance.py`
- Create: `docs/acceptance/evidence-index.md`

**Interfaces:**
- Uses two Labs, one general-research input, REST/A2A/MCP clients, Web browser, and one approval gate.

**Acceptance:** project-level criteria in the specification all pass; every Requirement ID points to test output, manifest, screenshot, query, or reviewed document.

**Evidence:** `docs/acceptance/evidence-index.md` plus machine-readable test reports.

- [ ] Write failing end-to-end scenarios before enabling staging dependencies.
- [ ] Run focused acceptance suite; record expected failures by unavailable component.
- [ ] Connect completed components without adding new behavior.
- [ ] Run the full suite and build evidence index; expect zero unexplained failure.
- [ ] Commit: `test: prove TOR acceptance`

### Deliverable 5.3 Operational documents

#### Task 18 Deliver architecture, runbook, user, admin, and API documentation

**Owner:** Technical Writer with Platform Lead  
**Effort:** M  
**Dependencies:** Tasks 1-17  
**Requirements:** DEL-010, CON-009

**Files:**
- Create: `docs/architecture/platform.md`
- Create: `docs/runbook/platform-operations.md`
- Create: `docs/user/research-workflow.md`
- Create: `docs/admin/lab-administration.md`
- Create: `docs/api/index.md`
- Create: `docs/acceptance/document-review.md`

**Interfaces:**
- Links deployed version, OpenAPI, Agent Card, MCP surface, dashboards, alerts, backup exclusions, and evidence index.

**Acceptance:** each TOR document exists, commands match staging, links resolve, and document review has no unsupported compliance/SLA claim.

**Evidence:** link checker, command smoke test, signed review checklist.

- [ ] Write document review checks for required headings, links, and forbidden unsupported claims.
- [ ] Run checks; expect failure because documents do not exist.
- [ ] Write documents from verified behavior and evidence only.
- [ ] Run link/command/review checks; expect pass.
- [ ] Commit: `docs: deliver platform operations guides`

## Dependency Order

```text
Task 1
  Task 2
    Task 3
      Task 4
    Task 5
      Task 6
        Task 7
          Task 8
            Tasks 9, 10, 11
              Task 12
                Tasks 13, 14, 15
                  Task 16
                    Task 17
                      Task 18
```

## Stop Condition

Stop when Task 17 proves all requirement IDs and Task 18 passes document review. Do not add providers, workflow engines, compliance programs, SLA, or product polish absent from the specification.
