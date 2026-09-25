from __future__ import annotations

import asyncio

import pytest

from scilab.infra.nats_events import NatsEventBus


class Message:
    def __init__(self, data: bytes) -> None:
        self.data = data


class FakeSubscription:
    def __init__(
        self,
        messages: list[bytes] | None = None,
        *,
        failure: Exception | None = None,
        wait: asyncio.Event | None = None,
    ) -> None:
        self._messages = messages or []
        self._failure = failure
        self._wait = wait
        self._index = 0
        self.messages = self
        self.started = asyncio.Event()
        self.unsubscribed = False

    def __aiter__(self) -> FakeSubscription:
        return self

    async def __anext__(self) -> Message:
        self.started.set()
        if self._wait is not None:
            await self._wait.wait()
        if self._failure is not None:
            raise self._failure
        if self._index == len(self._messages):
            raise StopAsyncIteration
        data = self._messages[self._index]
        self._index += 1
        return Message(data)

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class FakeNatsClient:
    def __init__(
        self,
        subscription: FakeSubscription | None = None,
        *,
        publish_failure: Exception | None = None,
        subscribe_failure: Exception | None = None,
    ) -> None:
        self.subscription = subscription or FakeSubscription()
        self.publish_failure = publish_failure
        self.subscribe_failure = subscribe_failure
        self.published: list[tuple[str, bytes]] = []
        self.subscribed: list[str] = []

    async def publish(self, subject: str, data: bytes) -> None:
        if self.publish_failure is not None:
            raise self.publish_failure
        self.published.append((subject, data))

    async def subscribe(self, subject: str) -> FakeSubscription:
        self.subscribed.append(subject)
        if self.subscribe_failure is not None:
            raise self.subscribe_failure
        return self.subscription


def test_publish_sends_bytes_to_the_exact_subject() -> None:
    async def scenario() -> None:
        client = FakeNatsClient()
        bus = NatsEventBus(client)

        await bus.publish("scilab.lab-a.run-1", b"event-bytes")

        assert client.published == [("scilab.lab-a.run-1", b"event-bytes")]

    asyncio.run(scenario())


def test_subscribe_preserves_order_and_unsubscribes_on_context_exit() -> None:
    async def scenario() -> None:
        subscription = FakeSubscription([b"first", b"second", b"third"])
        client = FakeNatsClient(subscription)
        bus = NatsEventBus(client)
        received: list[bytes] = []

        async with bus.subscribe("scilab.lab-a.run-1") as events:
            async for event in events:
                received.append(event)

        assert client.subscribed == ["scilab.lab-a.run-1"]
        assert received == [b"first", b"second", b"third"]
        assert subscription.unsubscribed

    asyncio.run(scenario())


def test_publish_surfaces_client_failure() -> None:
    async def scenario() -> None:
        failure = RuntimeError("publish failed")
        bus = NatsEventBus(FakeNatsClient(publish_failure=failure))

        with pytest.raises(RuntimeError, match="publish failed") as caught:
            await bus.publish("scilab.lab-a.run-1", b"event-bytes")

        assert caught.value is failure

    asyncio.run(scenario())


def test_subscribe_surfaces_client_failure() -> None:
    async def scenario() -> None:
        failure = RuntimeError("subscribe failed")
        bus = NatsEventBus(FakeNatsClient(subscribe_failure=failure))

        with pytest.raises(RuntimeError, match="subscribe failed") as caught:
            async with bus.subscribe("scilab.lab-a.run-1"):
                pass

        assert caught.value is failure

    asyncio.run(scenario())


def test_message_failure_propagates_and_unsubscribes() -> None:
    async def scenario() -> None:
        failure = RuntimeError("message stream failed")
        subscription = FakeSubscription(failure=failure)
        bus = NatsEventBus(FakeNatsClient(subscription))

        with pytest.raises(RuntimeError, match="message stream failed") as caught:
            async with bus.subscribe("scilab.lab-a.run-1") as events:
                await anext(events)

        assert caught.value is failure
        assert subscription.unsubscribed

    asyncio.run(scenario())


def test_cancellation_unsubscribes() -> None:
    async def scenario() -> None:
        subscription = FakeSubscription(wait=asyncio.Event())
        bus = NatsEventBus(FakeNatsClient(subscription))

        async def consume() -> None:
            async with bus.subscribe("scilab.lab-a.run-1") as events:
                await anext(events)

        task = asyncio.create_task(consume())
        await subscription.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert subscription.unsubscribed

    asyncio.run(scenario())
