---
name: artifact-store
description: Use when registering, exporting, or citing Run artifacts under the platform artifact and provenance contracts.
---

# Artifact Store

Treat every artifact as immutable evidence identified by its contract metadata and referenced by ID.

## Contract

- An `artifact.registered` payload contains `artifact_id`, `kind`, `uri`, `sha256`, `bytes`, and `produced_by_step`.
- Preserve the artifact bytes represented by `sha256` and `bytes`; do not replace a registered artifact in place.
- Run-manifest inputs contain `artifact_id`, `sha256`, and `name`. Step outputs, report references, and claim evidence use artifact IDs.
- Keep the URI as supplied by the contract. Do not invent storage endpoints, credentials, or access semantics.
- Register scripts, logs, outputs, citations, and reports only when their producer step and integrity metadata are known.

## Scenario proof

- Allowed: a produced report is registered with its kind, URI, SHA-256, byte count, and producing step, then referenced by a claim or completion record.
- Denied: an artifact with missing hash, byte count, producer, or unredacted secret content is not accepted as evidence.
- Edge: a binary artifact is exported byte-for-byte and its recorded hash and size remain unchanged; text decoding is not used as a substitute for binary export.

Requirement IDs: FR-005, FR-012, FR-017, NFR-005, NFR-006, CR-006, CR-008, CR-009, DEL-004, CON-006, CON-011
