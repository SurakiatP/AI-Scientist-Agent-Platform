from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from scilab.contracts import RunEvent
from scilab.identity import Identity


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity() -> Identity:
    return Identity("lab-a", "user:lab-a", frozenset({"runs:read"}))


def event(seq: int, *, payload: dict[str, object] | None = None, event_type: str = "run.state") -> RunEvent:
    return RunEvent.model_validate(
        {
            "event_id": f"01J000000000000000000000{seq:02d}",
            "run_id": "run-1",
            "lab_id": "lab-a",
            "ts": NOW,
            "type": event_type,
            "seq": seq,
            "payload": payload or {"from": "queued", "to": "running", "reason": "started"},
            "source": "run-service",
        }
    )


class Notifications:
    def __init__(self) -> None:
        self.pending: list[bytes] = []

    def push(self, value: bytes = b"wake") -> None:
        self.pending.append(value)

    def __aiter__(self) -> "Notifications":
        return self

    async def __anext__(self) -> bytes:
        if self.pending:
            return self.pending.pop(0)
        await asyncio.Future()
        raise AssertionError("unreachable")


class FakeService:
    def __init__(self, events: list[RunEvent]) -> None:
        self.events = events
        self.notifications = Notifications()
        self.calls: list[tuple[object, ...]] = []

    @asynccontextmanager
    async def _subscription(self):
        self.calls.append(("subscribed",))
        yield self.notifications

    def subscribe(self, _identity: Identity, _run_id: str):
        return self._subscription()

    def replay_events(self, _identity: Identity, _run_id: str, from_seq: int = 0) -> list[RunEvent]:
        self.calls.append(("replay", from_seq))
        return [item for item in self.events if item.seq > from_seq]


def test_stream_function_is_importable() -> None:
    from scilab.sse import stream_events

    assert stream_events


def test_replays_exclusively_and_formats_compact_sse_frame() -> None:
    from scilab.sse import stream_events

    service = FakeService([event(1), event(2, event_type="run.failed", payload={"reason": "error"})])
    stream = stream_events(service, identity(), "run-1", from_seq=1)

    async def run() -> str:
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(run())

    assert frame.startswith("id: 2\nevent: run.failed\ndata: ")
    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1]) == event(2, event_type="run.failed", payload={"reason": "error"}).model_dump(mode="json", by_alias=True)
    assert service.calls == [("subscribed",), ("replay", 1)]


def test_subscription_is_established_before_initial_replay() -> None:
    from scilab.sse import stream_events

    service = FakeService([event(1)])
    stream = stream_events(service, identity(), "run-1")

    async def run() -> None:
        try:
            await anext(stream)
        finally:
            await stream.aclose()

    asyncio.run(run())

    assert service.calls[:2] == [("subscribed",), ("replay", 0)]


def test_bus_wakeup_rereads_persisted_events_after_last_sequence() -> None:
    from scilab.sse import stream_events

    service = FakeService([event(1)])
    stream = stream_events(service, identity(), "run-1")

    async def run() -> str:
        try:
            await anext(stream)
            service.events.append(event(2))
            service.notifications.push()
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(run())
    asyncio.run(stream.aclose())

    assert "id: 2\n" in frame
    assert service.calls[-1] == ("replay", 1)


def test_timeout_requeries_before_emitting_exact_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    from scilab import sse

    service = FakeService([event(1)])
    async def timeout(awaitable: object, *, timeout: float):
        assert timeout == 15
        if hasattr(awaitable, "close"):
            awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(sse.asyncio, "wait_for", timeout)
    stream = sse.stream_events(service, identity(), "run-1")

    async def run() -> str:
        try:
            await anext(stream)
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(run())
    asyncio.run(stream.aclose())

    assert frame == ": heartbeat\n\n"
    assert service.calls[-1] == ("replay", 1)


def test_timeout_emits_new_persisted_event_instead_of_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    from scilab import sse

    service = FakeService([event(1)])
    wait_for_calls = 0
    async def timeout(awaitable: object, *, timeout: float):
        nonlocal wait_for_calls
        wait_for_calls += 1
        assert timeout == 15
        if hasattr(awaitable, "close"):
            awaitable.close()
        if wait_for_calls == 1:
            service.events.append(event(2))
        else:
            service.events = [event(1)]
        raise asyncio.TimeoutError

    monkeypatch.setattr(sse.asyncio, "wait_for", timeout)
    stream = sse.stream_events(service, identity(), "run-1")

    async def run() -> tuple[str, str]:
        try:
            await anext(stream)
            first = await anext(stream)
            second = await anext(stream)
            return first, second
        finally:
            await stream.aclose()

    frame, next_frame = asyncio.run(run())
    asyncio.run(stream.aclose())

    assert frame.startswith("id: 2\n")
    assert next_frame == ": heartbeat\n\n"
    assert wait_for_calls == 2
    assert service.calls[-1] == ("replay", 2)


def test_stream_propagates_read_authorization_and_tenant_errors() -> None:
    from scilab.sse import stream_events
    from scilab.tenancy import AuthorizationError

    class DeniedService(FakeService):
        def subscribe(self, _identity: Identity, _run_id: str):
            raise AuthorizationError("missing scope")

    stream = stream_events(DeniedService([]), identity(), "run-1")
    with pytest.raises(AuthorizationError):
        asyncio.run(anext(stream))


class TrackedNotifications:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.started = asyncio.Event()
        self.cancelled = False

    def push(self, value: bytes = b"wake") -> None:
        self.queue.put_nowait(value)

    def __aiter__(self) -> "TrackedNotifications":
        return self

    async def __anext__(self) -> bytes:
        self.started.set()
        try:
            return await self.queue.get()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class TrackedService:
    def __init__(self, events: list[RunEvent]) -> None:
        self.events = events
        self.notifications = TrackedNotifications()
        self.subscription_open = False
        self.subscription_closed = False

    @asynccontextmanager
    async def _subscription(self):
        self.subscription_open = True
        try:
            yield self.notifications
        finally:
            self.subscription_closed = True

    def subscribe(self, _identity: Identity, _run_id: str):
        return self._subscription()

    def replay_events(self, _identity: Identity, _run_id: str, from_seq: int = 0) -> list[RunEvent]:
        return [item for item in self.events if item.seq > from_seq]


def test_live_subscription_survives_timeouts_and_cleans_up_pending_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scilab import sse

    service = TrackedService([event(1)])
    real_wait_for = asyncio.wait_for

    async def bounded_wait_for(awaitable: object, *, timeout: float):
        assert timeout == 15
        return await real_wait_for(awaitable, timeout=0.01)

    monkeypatch.setattr(sse.asyncio, "wait_for", bounded_wait_for)

    async def run() -> None:
        stream = sse.stream_events(service, identity(), "run-1")
        first = await anext(stream)
        assert first.startswith("id: 1\n")
        assert service.subscription_open
        assert not service.subscription_closed

        assert await anext(stream) == ": heartbeat\n\n"
        assert await anext(stream) == ": heartbeat\n\n"
        assert service.subscription_open
        assert not service.notifications.cancelled

        service.events.append(event(2, event_type="run.failed"))
        service.notifications.push()
        live = await anext(stream)
        assert live.startswith("id: 2\nevent: run.failed\n")
        assert service.subscription_open
        assert not service.notifications.cancelled

        consumer = asyncio.create_task(anext(stream))
        await real_wait_for(service.notifications.started.wait(), timeout=0.1)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        assert service.subscription_closed
        assert service.notifications.cancelled

    asyncio.run(run())
