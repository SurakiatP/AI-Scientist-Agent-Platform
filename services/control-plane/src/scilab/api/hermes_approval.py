"""Deliver a staged human decision to the Lab's Hermes Run."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import httpx

from scilab.approvals import ApprovalService, ApprovalStateError
from scilab.hermes import HermesClient, HermesReceiptConflict
from scilab.identity import Identity




class HermesApprovalUnavailable(RuntimeError):
    pass


class HermesApprovalDelivery:
    def __init__(self, approvals: ApprovalService, resolver: Any, transport: httpx.AsyncClient, events: Any = None):
        self.approvals = approvals
        self.resolver = resolver
        self.transport = transport
        self.events = events

    async def decide_approval(
        self, identity: Identity, approval_id: str, decision: str, *, note: str | None = None, run_id: str
    ) -> Any:
        request_id = self.approvals.hermes_request_id(identity, approval_id, run_id)
        if request_id is None:
            return self.approvals.decide_approval(identity, approval_id, decision, note=note, run_id=run_id)

        final = self.approvals.final_hermes_approval(identity, approval_id, decision, run_id=run_id)
        if final is not None:
            return final

        request_id = self.approvals.stage_hermes_decision(
            identity, approval_id, decision, note=note, run_id=run_id
        )
        vendor_run_id = self.approvals.hermes_run_id(identity, approval_id, run_id)
        try:
            resolve = getattr(self.resolver, "resolve", self.resolver)
            endpoint, api_key = await asyncio.to_thread(resolve, identity.lab_id)
            hermes = HermesClient(
                {identity.lab_id: endpoint}, api_key=api_key,
                session_id=secrets.token_urlsafe(24), session_key=secrets.token_urlsafe(24),
                transport=self.transport,
            )
            choice = "once" if decision == "approve" else "deny"
            receipt = await hermes.approval_receipt(identity.lab_id, vendor_run_id, request_id)
            if receipt is None:
                try:
                    await hermes.respond_approval(identity.lab_id, vendor_run_id, request_id, decision)
                except (ValueError, httpx.HTTPError, OSError):
                    pass  # Reconcile ambiguous POST with the exact durable receipt.
                receipt = await hermes.approval_receipt(identity.lab_id, vendor_run_id, request_id)
            if receipt is None:
                raise HermesApprovalUnavailable("Hermes approval unavailable")
            if receipt["choice"] != choice:
                raise ApprovalStateError("Hermes approval conflict")
            if await hermes.run_status(identity.lab_id, vendor_run_id) != "running":
                raise HermesApprovalUnavailable("Hermes approval unavailable")
        except HermesReceiptConflict:
            raise ApprovalStateError("Hermes approval conflict") from None
        except ApprovalStateError:
            raise
        except Exception:
            raise HermesApprovalUnavailable("Hermes approval unavailable") from None
        try:
            return await self.approvals.confirm_hermes_decision(identity, approval_id, run_id=run_id)
        except ApprovalStateError:
            raise
        except Exception:
            raise HermesApprovalUnavailable("Hermes approval unavailable") from None
