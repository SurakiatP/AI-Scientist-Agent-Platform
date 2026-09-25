from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from scilab.events import EventBus


class NatsEventBus(EventBus):
    def __init__(self, client: Any) -> None:
        self._client = client

    async def publish(self, subject: str, data: bytes) -> None:
        await self._client.publish(subject, data)

    @asynccontextmanager
    async def subscribe(self, subject: str) -> AsyncIterator[AsyncIterator[bytes]]:
        subscription = await self._client.subscribe(subject)

        async def messages() -> AsyncIterator[bytes]:
            async for message in subscription.messages:
                yield message.data

        try:
            yield messages()
        finally:
            await subscription.unsubscribe()
