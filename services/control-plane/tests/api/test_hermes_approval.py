"""REST coordinator proof for exact Hermes approval reconciliation."""

import asyncio
from dataclasses import dataclass

import httpx
import pytest

from scilab.api.hermes_approval import HermesApprovalDelivery, HermesApprovalUnavailable
from scilab.approvals import ApprovalStateError
from scilab.identity import Identity


@dataclass
class Approvals:
    request_id: str | None = "vendor-1"
    vendor_run_id: str = "hermes-42"
    staged: tuple | None = None
    final: dict | None = None
    confirmation_failures: int = 0
    confirmations: int = 0

    def hermes_request_id(self, identity, approval_id, run_id):
        assert (identity.lab_id, approval_id, run_id) == ("lab-a", "approval-1", "run-1")
        return self.request_id

    def hermes_run_id(self, identity, approval_id, run_id):
        return self.vendor_run_id

    def final_hermes_approval(self, identity, approval_id, decision, *, run_id):
        if self.final and self.staged[1] != decision:
            raise ApprovalStateError("conflicting decision")
        return self.final

    def stage_hermes_decision(self, identity, approval_id, decision, *, note, run_id):
        if self.staged is None:
            self.staged = (identity.principal, decision, note, run_id)
        elif self.staged[1] != decision:
            raise ApprovalStateError("conflicting decision")
        return self.request_id

    async def confirm_hermes_decision(self, identity, approval_id, *, run_id):
        if self.confirmation_failures:
            self.confirmation_failures -= 1
            raise Exception("secret DB failure")
        self.confirmations += 1
        self.final = {
            "status": "approved" if self.staged[1] == "approve" else "rejected",
            "actor": self.staged[0], "note": self.staged[2],
        }
        return self.final

    def decide_approval(self, identity, approval_id, decision, *, note, run_id):
        return {"status": "approved" if decision == "approve" else "rejected"}


def actor(name="first"):
    return Identity("lab-a", f"user:{name}", {"runs:approve"})


def receipt(choice="once", run_id="hermes-42", request_id="vendor-1"):
    return {"object": "hermes.run.approval_response", "run_id": run_id,
            "request_id": request_id, "choice": choice, "resolved": 1}


def vendor(*, decision="approve", initial="missing", after="matching", post="ok", status="running"):
    requests = []
    choice = "once" if decision == "approve" else "deny"

    def answer(request):
        requests.append(request)
        if request.method == "POST":
            if post == "timeout":
                raise httpx.ReadTimeout("secret command")
            if post == "conflict":
                return httpx.Response(409, json={"detail": "secret command"})
            if post == "bad-ack":
                return httpx.Response(200, json=receipt(choice, request_id="wrong"))
            return httpx.Response(200, json=receipt(choice))
        if request.url.path.endswith("/vendor-1"):
            count = sum(r.url.path.endswith("/vendor-1") for r in requests)
            kind = initial if count == 1 else after
            if kind == "missing":
                return httpx.Response(404)
            if kind == "prepared":
                return httpx.Response(409)
            if kind == "opposite":
                return httpx.Response(200, json=receipt("deny" if choice == "once" else "once"))
            if kind == "mismatch":
                return httpx.Response(200, json=receipt(choice, run_id="other"))
            return httpx.Response(200, json=receipt(choice))
        return httpx.Response(200, json={"run_id": "hermes-42", "status": status})

    return httpx.MockTransport(answer), requests


@pytest.mark.parametrize(("decision", "choice"), [("approve", "once"), ("reject", "deny")])
def test_normal_decision_requires_receipt_and_running_status(decision, choice):
    approvals = Approvals()
    mock, requests = vendor(decision=decision)

    class Events:
        async def publish_event(self, *_):
            pytest.fail("coordinator duplicated the atomic run.state event")

    async def exercise():
        async with httpx.AsyncClient(transport=mock) as transport:
            delivery = HermesApprovalDelivery(approvals, lambda lab: ("http://hermes", "secret-key"), transport, Events())
            result = await delivery.decide_approval(actor(), "approval-1", decision, note="first note", run_id="run-1")
            assert result["status"] == ("approved" if decision == "approve" else "rejected")

    asyncio.run(exercise())
    assert [r.method for r in requests] == ["GET", "POST", "GET", "GET"]
    assert requests[1].url.path == "/v1/runs/hermes-42/approval"
    assert requests[1].headers["authorization"] == "Bearer secret-key"
    assert requests[1].read() == f'{{"choice":"{choice}","request_id":"vendor-1"}}'.encode()
    assert approvals.confirmations == 1


@pytest.mark.parametrize("post", ["timeout", "conflict", "bad-ack"])
def test_ambiguous_post_recovers_only_from_exact_receipt(post):
    approvals = Approvals()
    mock, requests = vendor(post=post)

    async def exercise():
        async with httpx.AsyncClient(transport=mock) as transport:
            delivery = HermesApprovalDelivery(approvals, lambda lab: ("http://hermes", "key"), transport)
            assert (await delivery.decide_approval(actor(), "approval-1", "approve", run_id="run-1"))["status"] == "approved"

    asyncio.run(exercise())
    assert [r.method for r in requests] == ["GET", "POST", "GET", "GET"]
    assert approvals.confirmations == 1


@pytest.mark.parametrize(("initial", "after", "status"), [
    ("missing", "missing", "running"),
    ("missing", "prepared", "running"),
    ("missing", "opposite", "running"),
    ("missing", "mismatch", "running"),
    ("matching", "matching", "interrupted"),
    ("matching", "matching", "completed"),
    ("prepared", "matching", "running"),
])
def test_unconfirmed_or_nonrunning_vendor_fails_closed(initial, after, status):
    approvals = Approvals()
    mock, requests = vendor(initial=initial, after=after, post="conflict", status=status)

    async def exercise():
        async with httpx.AsyncClient(transport=mock) as transport:
            delivery = HermesApprovalDelivery(approvals, lambda lab: ("http://hermes", "key"), transport)
            expected = ApprovalStateError if after in {"prepared", "opposite", "mismatch"} or initial == "prepared" else HermesApprovalUnavailable
            with pytest.raises(expected) as error:
                await delivery.decide_approval(actor(), "approval-1", "approve", run_id="run-1")
            assert "secret" not in str(error.value)

    asyncio.run(exercise())
    assert approvals.confirmations == 0
    assert requests[0].method == "GET"


def test_db_failure_retries_receipt_then_replays_original_final_without_network():
    approvals = Approvals(confirmation_failures=1)
    mock, requests = vendor()

    async def exercise():
        async with httpx.AsyncClient(transport=mock) as transport:
            delivery = HermesApprovalDelivery(approvals, lambda lab: ("http://hermes", "key"), transport)
            with pytest.raises(HermesApprovalUnavailable) as error:
                await delivery.decide_approval(actor(), "approval-1", "approve", note="first", run_id="run-1")
            assert "secret" not in str(error.value)
            assert approvals.confirmations == 0
            result = await delivery.decide_approval(actor("second"), "approval-1", "approve", note="second", run_id="run-1")
            assert result == {"status": "approved", "actor": "user:first", "note": "first"}
            count = len(requests)
            assert await delivery.decide_approval(actor("second"), "approval-1", "approve", run_id="run-1") == result
            assert len(requests) == count
            with pytest.raises(ApprovalStateError):
                await delivery.decide_approval(actor("second"), "approval-1", "reject", run_id="run-1")

    asyncio.run(exercise())
    assert [r.method for r in requests].count("POST") == 1
    assert approvals.confirmations == 1


def test_timeout_without_receipt_can_recover_on_later_retry_without_second_post():
    approvals = Approvals()
    requests = []

    def answer(request):
        requests.append(request)
        if request.method == "POST":
            raise httpx.ReadTimeout("secret command")
        if request.url.path.endswith("/vendor-1"):
            count = sum(r.url.path.endswith("/vendor-1") for r in requests)
            return httpx.Response(404) if count < 3 else httpx.Response(200, json=receipt())
        return httpx.Response(200, json={"run_id": "hermes-42", "status": "running"})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as transport:
            delivery = HermesApprovalDelivery(approvals, lambda lab: ("http://hermes", "key"), transport)
            with pytest.raises(HermesApprovalUnavailable):
                await delivery.decide_approval(actor(), "approval-1", "approve", note="first", run_id="run-1")
            assert (await delivery.decide_approval(actor("second"), "approval-1", "approve", note="second", run_id="run-1"))["actor"] == "user:first"

    asyncio.run(exercise())
    assert [r.method for r in requests].count("POST") == 1
    assert approvals.confirmations == 1


def test_platform_approval_does_not_resolve_hermes_secret():
    approvals = Approvals(request_id=None)

    async def exercise():
        delivery = HermesApprovalDelivery(approvals, lambda lab: pytest.fail("unexpected secret lookup"), None)
        return await delivery.decide_approval(actor(), "approval-1", "approve", run_id="run-1")

    assert asyncio.run(exercise())["status"] == "approved"
