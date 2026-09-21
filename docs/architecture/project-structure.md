# Project Structure and Delegation Boundaries

**Status:** Implementation organization approved for execution planning  
**Source:** `docs/specs/ai-scientist-agent-platform-spec.md`  
**Implementation plan:** `docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md`

## Structure

```text
AI-Scientist-Agent-Platform/
├── apps/
│   └── web/                         # Next.js user interface only
├── services/
│   ├── control-plane/               # Modular FastAPI application
│   └── lab-operator/                # Independent Kopf deployment
├── contracts/                       # OpenAPI, JSON Schema, A2A contracts
├── sdk/
│   └── python/                      # Thin generated/handwritten client
├── skills/
│   ├── platform/                    # Four TOR platform skills
│   └── packs/                       # Pinned selected upstream packs
├── deploy/
│   ├── helm/scilab/                 # Vendor-neutral Helm chart
│   └── policies/                    # OPA and network policies
├── tests/
│   └── acceptance/                  # Black-box TOR acceptance tests
├── docs/                            # TOR, specification, plan, guides, evidence
├── pyproject.toml                   # Shared Python tooling/workspace config
└── README.md                        # Entry point; links, not duplicated docs
```

Only three deployable runtimes exist initially: Web, Control Plane, and Lab
Operator. Logical control-plane services remain Python modules until measured
scaling, security, or ownership needs justify another deployment.

## Ownership and Dependency Rules

| Area | Owner role | May depend on | Must not depend on |
|---|---|---|---|
| `contracts/` | Backend Lead | Nothing at runtime | Apps or services |
| `services/control-plane/` | Backend team | `contracts/`, external infrastructure SDKs | Web or Lab Operator internals |
| `services/lab-operator/` | Platform team | Kubernetes APIs, CRDs/contracts | Control Plane internals |
| `apps/web/` | Frontend team | Published OpenAPI/SSE contracts | Python source modules |
| `sdk/python/` | API team | Published OpenAPI/SSE contracts | Service internals |
| `skills/` | AI Platform team | Pinned upstream source and platform contracts | Web source |
| `deploy/` | Platform/SRE | Built images and public configuration contracts | Application implementation imports |
| `tests/acceptance/` | QA | Deployed public interfaces | Private Python imports |
| `docs/` | Technical Writer/owning engineer | Verified behavior and evidence | Unverified claims |

Shared contracts flow outward:

```text
contracts -> control-plane -> REST / A2A / MCP / SSE
         \-> web and Python SDK (public contracts only)
control-plane <-> lab-operator (Kubernetes resource contract only)
deployed system -> black-box acceptance tests -> evidence index
```

## File and Change Discipline

- A delegated worker owns only the paths named in its task.
- Concurrent workers never edit the same file or shared lockfile.
- Root dependency files and generated contracts are changed by one worker per
  wave; the main agent resolves integration after that wave.
- Database migrations are append-only and use the task numbers reserved in the
  implementation plan.
- Cross-area behavior starts as a contract test before implementation.
- No `common`, `utils`, plugin framework, base repository, or extra service is
  added without two concrete callers and a TOR-backed need.
- Secrets, `.env` content, credentials, and production data are never delegated.

## Implementation Phases

| Phase | Tasks | Result | Delegate-build waves |
|---|---:|---|---|
| 1. Control Plane Foundation | 1-4 | Contracts, identity/tenant isolation, Run lifecycle, events/SSE | `1` -> `2` -> `3` -> `4` |
| 2. Lab Runtime and Skills | 5-8 | Kopf operator, OpenSandbox/Kata, governed skills, Hermes research cycle | `5` -> `6` -> `7` -> `8` |
| 3. Evidence and Governance | 9-11 | Provenance, approvals, metering/telemetry/audit | `9 + 10 + 11` in parallel |
| 4. Public Interfaces | 12-15 | REST/SDK, A2A, MCP, Web | `12` -> `13 + 14` -> `15` |
| 5. Delivery and Proof | 16-18 | Helm deployment, acceptance evidence, handoff docs | `16` -> `17` -> `18` |

Each phase uses `delegate-build`: `gpt-5.6-sol` with high effort validates the
wave plan and performs independent final review; `gpt-5.6-luna` with xhigh
effort implements non-overlapping worker tasks. A phase starts only after its
dependencies pass, and the next phase starts only after aggregate review is
`PASS`.

## Frontend Hallmark Gate

Task 15 starts with a Hallmark pre-flight scan. Before design work, ask the
owner once for Audience, single Use case, and an explicit Tone. Then:

1. State every frontend file to create or modify; request confirmation for any
   deletion.
2. Preserve discovered framework, palette, fonts, spacing, and motion stance.
3. Put all colors and fonts behind design tokens; do not invent TOR content or
   metrics.
4. Implement all eight interactive states and accessibility basics.
5. Verify widths 320, 375, 414, and 768 px and run the Hallmark slop test.
6. Store Hallmark project memory only when Task 15 actually emits a design.

The gate applies during frontend implementation, not during backend phases.

## Stop Rules

- Stop a phase on an unresolved contract, security, data-scope, or acceptance
  decision and ask the owner.
- Do not add Temporal, Daytona, Nextflow, cron literature watch, data residency,
  extra providers, compliance certification, or unspecified SLA behavior.
- Stop implementation when Tasks 17 and 18 prove all TOR acceptance criteria;
  product polish beyond that point is a separate decision.
