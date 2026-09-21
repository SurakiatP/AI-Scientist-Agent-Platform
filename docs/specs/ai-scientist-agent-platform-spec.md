# AI Scientist Agent Platform Specification

**Source:** `docs/TOR/TOR_AI_Scientist_Agent_Platform_v1.0.docx`

**Requirement analysis:** `docs/TOR/TOR_Requirement_Analysis_v1.0.md`

**Status:** Approved for implementation planning on 21 September 2026

## Problem

Scientific research work spans literature review, public databases, specialist analysis tools, code execution, review, and evidence-backed reporting. Hermes Agent and Scientific Agent Skills supply agent behavior, but do not supply the organizational platform required by the TOR: tenant isolation, secure execution, provenance, cost control, approval, audit, and REST/A2A/MCP access. Source: TOR page 3 section 1.

## Solution

Build a thin Python control plane around Hermes. One Hermes PI instance serves each Lab. All Web, REST, A2A, and MCP requests become one Run resource after identity normalization. Code-capable delegated agents receive ephemeral OpenSandbox sandboxes on Kubernetes using Kata RuntimeClass, default-deny egress, and Credential Vault. Results leave the sandbox through the Artifact Service and are sealed in a provenance manifest. Non-sensitive heavy Python work may use Modal. Source: TOR pages 4-22, sections 3, 5.1, A.1-A.6.

Initial implementation uses a modular FastAPI control-plane deployable, a separate Kopf Lab Operator, the OpenSandbox Kubernetes operator, and a Next.js web client. Logical service boundaries from the TOR remain explicit modules and contracts; separate deployments are deferred until measured scaling or security ownership requires them.

## Confirmed Decisions

| Decision | Result | Basis |
|---|---|---|
| Scientific skills upstream | `K-Dense-AI/scientific-agent-skills` pinned to `v2.69.0` | Owner decision; TOR ADR-006 |
| Initial skill coverage | Selected general-research packs only; do not load all 166 skills | Owner decision; upstream security guidance; TOR ADR-006 |
| Backend | Python 3.13, FastAPI | Owner decision; TOR A.2 and A.3.2 |
| Lab operator | Kopf | Owner decision; TOR A.2 allows Kopf or Kubebuilder |
| Login | Keycloak OIDC/OAuth 2.1 with owner/researcher/viewer roles | Owner decision; TOR pages 5, 10, 18 |
| Sandbox | OpenSandbox `release-1.1.0`, Python SDK, Kubernetes operator | Owner decision; satisfies TOR sandbox role |
| Secure runtime | Kata RuntimeClass | Owner decision; TOR allows Kata; OpenSandbox egress requires Kata rather than gVisor |
| Sandbox allocation | Create an ephemeral sandbox on demand for each delegated agent that executes code or external tools | Owner decision; tighter than TOR per-run minimum |
| Sensitive compute | Keep inside Kubernetes/OpenSandbox | TOR page 6 limits external terminal backends to non-sensitive data |
| Non-sensitive heavy compute | Modal | Owner decision; TOR page 6 permits Modal |
| Deferred compute | No Daytona or Nextflow in initial delivery | Lean initial scope; add only for persistent workspaces or repeatable multi-stage pipelines |
| A2A authentication | Registered peer bearer token over TLS | Owner decision; TOR A.6.1 permits bearer or mTLS |
| Capacity | Use all TOR defaults; verify tenant isolation with at least two Labs; make no extra SLA promise | Owner decision |
| Environments | Local development, staging Kubernetes, production Kubernetes; vendor-neutral Helm | Owner decision; TOR does not select cloud/vendor |
| External compliance | Do not claim PDPA, ISO, or other certification absent from TOR | TOR omission |

## Architecture Decisions

ADR-001 through ADR-007 from TOR pages 8-9 remain binding:

1. Hermes is one PI orchestrator per Lab; specialists use delegation.
2. Research enters through Hermes Runs API, not chat completions.
3. Platform Gateway maps REST, A2A, and MCP to one Run resource.
4. Platform MCP uses FastMCP.
5. Platform A2A uses `a2a-sdk`; Hermes A2A remains internal.
6. Skills pass pin, scan, pack, evaluation, image build, and read-only mount.
7. Model access passes through OpenRouter/LiteLLM; data residency remains Phase 2.

## User Stories

- As a researcher, I submit a goal, inputs, skill packs, and budget; follow progress and cost; approve blocked actions; and receive evidence-backed results. `FR-001`-`FR-005`
- As a Lab owner, I manage members, keys, peers, budgets, and usage. `FR-006`, `FR-007`
- As an external system or agent, I create and follow the same Run through REST, A2A, or MCP under equivalent identity and scopes. `FR-008`, `FR-020`-`FR-022`, `FR-027`, `FR-028`
- As a reviewer or auditor, I trace each claim to citation, artifact, command log, image/config hash, actor, source, and cost. `FR-005`, `FR-012`, `FR-013`, `FR-025`, `NFR-009`
- As an administrator, I provision isolated Labs, enforce approval/network/skill policy, and correlate telemetry. `FR-010`, `FR-011`, `FR-016`, `FR-023`, `FR-024`

## Functional Behavior

The normative functional behavior is the Requirement Matrix `FR-001` through `FR-030`. Key flows:

1. Submit: validate identity, Lab, quota, budget, inputs, skill packs, and idempotency key; return `202` and Run.
2. Execute: queue Run, call Lab Hermes Runs API, normalize events, delegate specialist work, and allocate OpenSandbox only when an agent needs code/tools.
3. Approve: pause policy-blocked actions, expose preview/reason/expiry, then resume or cancel from owner decision.
4. Preserve: register inputs, scripts, logs, outputs, citations, claims, and costs; seal manifest before completion.
5. Deliver: expose report and artifacts through Web, REST, A2A, and MCP.

## Non Functional Behavior

The normative non-functional behavior is `NFR-001` through `NFR-012`:

- Strict Lab isolation covers data, rights, cost, secrets, network, and compute.
- Hermes ports 8642 and 9900 remain ClusterIP-only.
- OpenSandbox uses Kata, default-deny egress, no service-mesh sidecar injection, and Credential Vault rather than plaintext sandbox secrets.
- Queue timeout is 30 minutes; Run default is 120 minutes; approval default is 24 hours; heartbeat loss is 90 seconds; retry maximum is two.
- SSE heartbeat is 15 seconds and supports `from_seq`; artifact URL lifetime is 15 minutes.
- Skills require Python 3.13+, weekly scanning, per-skill license inventory, and read-only runtime mount.
- Tool arguments are redacted before event, log, or audit storage.

## Deliverables

`DEL-001` through `DEL-010` are required:

1. Web application.
2. Control plane modules and Lab Operator.
3. Pinned Hermes image/config/hooks.
4. Selected Scientific Agent Skill packs and four platform skills.
5. OpenSandbox/Kata integration and Modal path for non-sensitive heavy jobs.
6. REST/OpenAPI/SSE and Python SDK example.
7. A2A server and Lab Agent Cards.
8. MCP Streamable HTTP server.
9. OTel/Langfuse, Prometheus/Grafana, Loki, and LiteLLM integration.
10. Architecture, runbook, user, administrator, and API documentation.

## Testing Decisions

- Write contract and state tests before implementation.
- Use two Labs for cross-tenant negative tests.
- Use deterministic clocks for timeout/retry/approval tests.
- Run REST, A2A, and MCP against the same Run fixture and compare identity/state/artifact results.
- Run untrusted-code escape, egress-deny, credential-redaction, and Hermes-port network tests.
- Verify every completed Run has a sealed manifest and every claim has evidence.
- Verify UI behavior with browser-level submit, streaming, approval, result, and Lab-admin scenarios.
- Treat security scanner output as review input, not certification.

## Acceptance Criteria

1. One research workflow completes from Web submission to sealed report and manifest.
2. REST, A2A, and MCP expose equivalent Run behavior with correct scopes.
3. Cross-Lab reads, writes, approvals, artifacts, keys, compute, and usage fail.
4. State, timeout, retry, approval, cancellation, SSE resume, and cost events match TOR.
5. Hermes is inaccessible outside allowed cluster callers.
6. OpenSandbox agent execution uses Kata, default-deny egress, and Credential Vault.
7. Skill image proves pin, scan, evaluation, license inventory, and read-only mount.
8. Every `FR`, `NFR`, `CR`, `DEL`, and `CON` ID maps to implementation task and acceptance evidence.

## Out of Scope

- New agent framework.
- Loading all 166 upstream skills in initial delivery.
- Daytona and Nextflow in initial delivery.
- Temporal and data residency implementation, marked Phase 2 in TOR.
- Cron literature watch, marked P3 in TOR.
- External compliance certification, unspecified SLA, backup/DR targets, retention policy, RPO/RTO, or cloud-vendor commitment.
- Product features and API surfaces absent from the Requirement Matrix.
