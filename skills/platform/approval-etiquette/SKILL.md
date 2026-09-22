---
name: approval-etiquette
description: Use when a Run reaches a human approval gate or an action may be resumed, rejected, or expired.
---

# Approval Etiquette

Treat approval as an explicit, previewed decision attached to the Run lifecycle; never infer consent from progress or silence.

## Contract

- An `approval.required` payload contains `approval_id`, `action`, `reason`, `policy_rule`, `expires_at`, and object `preview`.
- `expires_at` is an ISO 8601 timestamp with timezone. Preserve the action and policy rule shown to the approver.
- A Run awaiting approval remains in `awaiting_approval` until the existing lifecycle records a permitted decision. Do not execute the gated action while approval is absent, rejected, or expired.
- Keep approval events attributable to the Run and use the event contract; do not invent credentials, endpoints, or policy actions.

## Scenario proof

- Allowed: pause a Run, show the approval ID, reason, policy rule, expiry, and preview, then resume only after the corresponding decision is recorded.
- Denied: missing approval, an expired approval, or a decision for an action different from the preview cannot authorize execution.
- Edge: an approval that expires while the Run is waiting follows the existing `approval_expired` lifecycle reason and does not run the gated action.

Requirement IDs: FR-003, FR-009, FR-011, FR-017, NFR-005, NFR-006, CR-005, CR-006, CR-009, DEL-004, CON-006, CON-011
