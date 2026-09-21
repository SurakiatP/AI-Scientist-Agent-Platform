# TOR Traceability Verification for AI Scientist Agent Platform

วันที่ตรวจ 21 กันยายน 2569

## Scope

ตรวจเอกสารต่อไปนี้โดยไม่แก้ product code และไม่เพิ่ม scope:

- `docs/TOR/TOR_Requirement_Analysis_v1.0.md`
- `docs/specs/ai-scientist-agent-platform-spec.md`
- `docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md`

## Proof Commands

```bash
rg -o '(FR|NFR|CR|DEL|CON)-[0-9]{3}' docs/TOR/TOR_Requirement_Analysis_v1.0.md | sort -u
rg -o '(FR|NFR|CR|DEL|CON)-[0-9]{3}' docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md | sort -u
diff -u /tmp/tor-required-ids.txt /tmp/tor-plan-ids.txt
rg -c '^#### Task [0-9]+' docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md
rg -c '^\*\*(Owner|Effort|Dependencies|Requirements|Acceptance|Evidence):\*\*' docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md
rg -n 'TBD|TODO|implement later|fill in|appropriate error|Similar to Task' docs/specs/ai-scientist-agent-platform-spec.md docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md
git diff --check -- docs/TOR/TOR_Requirement_Analysis_v1.0.md docs/specs/ai-scientist-agent-platform-spec.md docs/superpowers/plans/2026-09-21-ai-scientist-agent-platform.md
```

## Results

### Pass

- Requirement set contains 72 unique IDs: 30 Functional, 12 Non-functional, 9 Compliance, 10 Deliverable, and 11 Constraint IDs.
- Plan contains the same 72 IDs. `diff` returned no missing or extra ID.
- Plan contains 18 tasks.
- Every task has owner role, effort estimate, dependencies, Requirement IDs, acceptance criteria, and evidence.
- Specification contains Problem, Solution, User Stories, Confirmed Decisions, Testing Decisions, Deliverables, and Out of Scope.
- Placeholder scan returned no prohibited placeholder.
- `git diff --check` returned no whitespace error.
- No issue and no product code were created.

### Fail

- None.

### Unresolved but Accepted

- TOR does not state production user count, Lab count, maximum input size, storage quota, throughput, or SLA. Plan keeps TOR runtime defaults and makes no extra capacity promise.
- TOR does not select cloud, Kubernetes distribution, or organizational IdP. Specification stays vendor-neutral and uses Keycloak-local accounts until federation is configured.
- TOR does not define PDPA, ISO, retention, backup/DR, RPO/RTO, or penetration-test acceptance. These remain outside initial scope and must not be claimed.
- Named staff are unknown. Plan assigns owner roles, not people.

### Risks

| Risk | Control in plan | Trigger for new decision |
|---|---|---|
| Scientific Agent Skills and OpenSandbox move quickly and support current releases | Pin v2.69.0 and release-1.1.0; record source/image digests; scan/evaluate before promotion | Security fix or incompatible upstream release |
| Per-agent sandbox allocation increases startup time and cluster cost | Allocate only when an agent needs code/tools; destroy at terminal state | Measured latency or cost misses project needs |
| OpenSandbox Credential Vault and strict egress require Kata and exclude transparent service-mesh sidecars | Kata RuntimeClass, default-deny `dns+nft`, no mesh injection on sandbox pods | Cluster cannot provide Kata/nft support |
| Modal receives only non-sensitive workloads | Classification gate before dispatch; sensitive work stays in Kubernetes | TOR is formally amended for external sensitive processing |
| Bearer-based A2A depends on TLS and token handling | Registered peer token, least scopes, hashed storage, rotation runbook | Organization requires mTLS/PKI |

## Final Status

**PASS with accepted unresolved items.** Planning acceptance is complete. Implementation acceptance remains unverified until tasks execute and produce the listed evidence.
