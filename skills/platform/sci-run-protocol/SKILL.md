---
name: sci-run-protocol
description: Use when scientific work must run or delegate through the platform Run, event, and research-result contracts.
---

# Sci-Run Protocol

Keep one research request inside one Run. Preserve its `run_id` and `lab_id`, use the Run lifecycle, and expose progress through normalized events.

## Contract

- Use the Run states and transitions already defined by the platform; research does not use a completion-only path.
- Handle only the declared RunEvent types. Preserve `event_id`, `run_id`, `lab_id`, `ts`, `type`, `seq`, `payload`, and `source`; keep `seq` ordered per Run.
- Tool events use `tool`, `args_redacted`, `duration_ms`, `ok`, and optional `error`. Never place secrets in event arguments.
- Delegation events use `delegation_id`, `role`, `goal`, `child_count`, and optional `schema_valid`.
- Return a `ResearchResult` with `claims`, `artifacts`, and `caveats`; each claim has `text`, `evidence`, and confidence from 0 to 1.

## Scenario proof

- Allowed: a Run records Plan, Gather, Analyze, Critique, and Report progress, delegates a role with the delegation payload, and returns claims linked to evidence.
- Denied: a request to bypass the Run lifecycle, emit unredacted tool arguments, or accept a delegated result without the declared result shape is rejected.
- Edge: if delegated output remains invalid after the permitted schema-repair attempt, retain `schema_valid: false` and do not present it as a valid result.

Requirement IDs: FR-009, FR-015, FR-017, FR-025, FR-029, FR-030, NFR-005, NFR-006, CR-005, CR-006, DEL-004, CON-002, CON-006, CON-011
