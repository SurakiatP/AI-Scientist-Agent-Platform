---
name: provenance-manifest
description: Use when assembling, validating, or sealing the RunManifest and claim-to-evidence record for a scientific Run.
---

# Provenance Manifest

Seal a complete, contract-valid record of the Run inputs, delegated steps, claims, costs, and immutable build identities.

## Contract

- A `RunManifest` contains `run_id`, `lab_id`, `actor`, `source`, `goal`, `skill_packs`, `hermes`, `skills_image`, `sandbox_image`, `inputs`, `steps`, `claims`, `cost`, `sealed_at`, and `manifest_sha256`.
- `source` is one of `web`, `rest`, `a2a`, or `mcp`. Hermes records `image`, `config_sha256`, and `model_aliases` with `pi` and `child`.
- Each input has `artifact_id`, `sha256`, and `name`; each step has `n`, `role`, `delegation_id`, `commands_log`, and `outputs`.
- Each claim has `id`, `text`, `evidence`, and confidence from 0 to 1. Every delivered claim must point to evidence.
- Record `tokens_in`, `tokens_out`, `llm_thb`, and `compute_thb`; seal only after validation and retain `sealed_at` plus `manifest_sha256`.

## Scenario proof

- Allowed: a completed Run seals inputs, step outputs, claims with evidence, pinned image/config identities, cost, and the manifest hash.
- Denied: a manifest with a missing required field, unsupported source, out-of-range confidence, or claim without evidence is not sealed.
- Edge: after sealing, any field or referenced artifact changes the verification result; produce a new valid manifest rather than silently mutating the sealed one.

Requirement IDs: FR-005, FR-012, FR-014, FR-017, FR-025, FR-030, NFR-005, NFR-006, CR-005, CR-006, CR-008, DEL-004, CON-006, CON-011
