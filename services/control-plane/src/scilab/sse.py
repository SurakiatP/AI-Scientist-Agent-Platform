from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from scilab.contracts import RunEvent
from scilab.identity import Identity


HEARTBEAT_SECONDS = 15


def _frame(event: RunEvent) -> str:
    data = json.dumps(
        event.model_dump(mode="json", by_alias=True),
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"id: {event.seq}\nevent: {event.type}\ndata: {data}\n\n"


async def stream_events(
    service: Any,
    identity: Identity,
    run_id: str,
    from_seq: int = 0,
) -> AsyncIterator[str]:
    last_seq = from_seq
    async with service.subscribe(identity, run_id) as notifications:
        for event in service.replay_events(identity, run_id, last_seq):
            if event.seq > last_seq:
                last_seq = event.seq
                yield _frame(event)

        notification_task: asyncio.Task[Any] | None = asyncio.create_task(anext(notifications))
        try:
            while True:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(notification_task),
                        timeout=HEARTBEAT_SECONDS,
                    )
                except asyncio.TimeoutError:
                    pending = service.replay_events(identity, run_id, last_seq)
                    if not pending:
                        yield ": heartbeat\n\n"
                    else:
                        for event in pending:
                            if event.seq > last_seq:
                                last_seq = event.seq
                                yield _frame(event)
                    continue
                except StopAsyncIteration:
                    return
                else:
                    notification_task = asyncio.create_task(anext(notifications))
                    pending = service.replay_events(identity, run_id, last_seq)
                    for event in pending:
                        if event.seq > last_seq:
                            last_seq = event.seq
                            yield _frame(event)
        finally:
            if notification_task is not None and not notification_task.done():
                notification_task.cancel()
                try:
                    await notification_task
                except asyncio.CancelledError:
                    pass
