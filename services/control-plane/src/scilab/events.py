from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, contextmanager
from datetime import datetime, timezone
from typing import Any, Protocol

from scilab.contracts import RunEvent
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.service import RunNotFound
from scilab.tenancy import require_scope


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_INDEX = {character: index for index, character in enumerate(_ULID_ALPHABET)}
_ENTROPY_MASK = (1 << 80) - 1
_TIMESTAMP_MASK = (1 << 48) - 1
_TOOL_EVENTS = {"tool.started", "tool.progress", "tool.finished"}
_SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
    "cookie",
    "client_secret",
    "private_key",
}


class EventBus(Protocol):
    async def publish(self, subject: str, data: bytes) -> None: ...

    def subscribe(self, subject: str) -> AbstractAsyncContextManager[AsyncIterator[bytes]]: ...


class EventFanoutError(RuntimeError):
    def __init__(self, event: RunEvent, cause: Exception) -> None:
        super().__init__(f"event {event.event_id} persisted but fan-out failed: {cause}")
        self.event = event
        self.cause = cause


def _subject(lab_id: str, run_id: str) -> str:
    for token in (lab_id, run_id):
        if not isinstance(token, str) or not token.strip() or any(
            character.isspace() or character in ".*>" for character in token
        ):
            raise ValueError("NATS subject tokens must be non-blank and contain no separators")
    return f"runs.{lab_id}.{run_id}"


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[REDACTED]" if _is_sensitive_key(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact(item) for item in value)
    return value


def _is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith("_token") or lowered.endswith("_secret")


def _event_payload(event_type: object, payload: Mapping[str, Any]) -> dict[str, Any]:
    prepared = dict(payload)
    if event_type in _TOOL_EVENTS:
        if "args" in prepared:
            raise ValueError("raw tool args are not accepted; use args_redacted")
        if "args_redacted" in prepared:
            prepared["args_redacted"] = _redact(prepared["args_redacted"])
    return prepared


def _row_values(row: Mapping[str, Any] | tuple[Any, ...], columns: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(row, Mapping):
        return {column: row[column] for column in columns}
    return dict(zip(columns, row, strict=True))


def _encode_ulid(timestamp_ms: int, entropy: int) -> str:
    if not 0 <= timestamp_ms <= _TIMESTAMP_MASK or not 0 <= entropy <= _ENTROPY_MASK:
        raise ValueError("ULID component out of range")
    value = (timestamp_ms << 80) | entropy
    return "".join(_ULID_ALPHABET[(value >> shift) & 31] for shift in range(125, -1, -5))


def _decode_ulid(event_id: str) -> tuple[int, int]:
    if len(event_id) != 26 or any(character not in _ULID_INDEX for character in event_id):
        raise ValueError("invalid ULID")
    value = 0
    for character in event_id:
        value = (value << 5) | _ULID_INDEX[character]
    return value >> 80, value & _ENTROPY_MASK


def _next_ulid(previous: str | None, now: datetime, entropy: Callable[[int], bytes]) -> str:
    now_ms = max(0, int(now.astimezone(timezone.utc).timestamp() * 1000))
    if previous is None:
        random_bytes = entropy(10)
        if len(random_bytes) != 10:
            raise ValueError("ULID entropy must be exactly 10 bytes")
        return _encode_ulid(min(now_ms, _TIMESTAMP_MASK), int.from_bytes(random_bytes, "big"))

    previous_ms, previous_entropy = _decode_ulid(previous)
    if now_ms > previous_ms:
        random_bytes = entropy(10)
        if len(random_bytes) != 10:
            raise ValueError("ULID entropy must be exactly 10 bytes")
        return _encode_ulid(min(now_ms, _TIMESTAMP_MASK), int.from_bytes(random_bytes, "big"))
    if previous_entropy < _ENTROPY_MASK:
        return _encode_ulid(previous_ms, previous_entropy + 1)
    if previous_ms == _TIMESTAMP_MASK:
        raise ValueError("ULID logical clock exhausted")
    return _encode_ulid(previous_ms + 1, 0)


class EventService:
    _EVENT_COLUMNS = ("event_id", "run_id", "lab_id", "ts", "type", "seq", "payload", "source")

    def __init__(
        self,
        connection: Any,
        bus: EventBus,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        entropy: Callable[[int], bytes] = os.urandom,
    ) -> None:
        self.connection = connection
        self.bus = bus
        self.clock = clock
        self.entropy = entropy

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    @staticmethod
    def _run(cursor: Any, identity: Identity, run_id: str, *, lock: bool) -> None:
        suffix = " FOR UPDATE" if lock else ""
        cursor.execute(
            "SELECT id, lab_id FROM runs WHERE lab_id = %s AND id = %s" + suffix,
            (identity.lab_id, run_id),
        )
        if cursor.fetchone() is None:
            raise RunNotFound("run not found")

    @classmethod
    def _event_from_row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> RunEvent:
        values = _row_values(row, cls._EVENT_COLUMNS)
        if isinstance(values["payload"], (str, bytes, bytearray)):
            values["payload"] = json.loads(values["payload"])
        return RunEvent.model_validate(values)

    async def publish_event(
        self,
        identity: Identity,
        run_id: str,
        event_type: object,
        payload: Mapping[str, Any],
        source: object,
    ) -> RunEvent:
        subject = _subject(identity.lab_id, run_id)
        prepared = _event_payload(event_type, payload)
        with self._access(identity, "runs:write") as cursor:
            self._run(cursor, identity, run_id, lock=True)
            cursor.execute(
                "SELECT event_id, seq FROM run_events "
                "WHERE lab_id = %s AND run_id = %s ORDER BY seq DESC LIMIT 1",
                (identity.lab_id, run_id),
            )
            latest = cursor.fetchone()
            latest_values = _row_values(latest, ("event_id", "seq")) if latest is not None else None
            seq = int(latest_values["seq"]) + 1 if latest_values else 1
            now = self.clock()
            event_id = _next_ulid(latest_values["event_id"] if latest_values else None, now, self.entropy)
            event = RunEvent.model_validate(
                {
                    "event_id": event_id,
                    "run_id": run_id,
                    "lab_id": identity.lab_id,
                    "ts": now,
                    "type": event_type,
                    "seq": seq,
                    "payload": prepared,
                    "source": source,
                }
            )
            event_json = event.model_dump(mode="json", by_alias=True)
            sql_payload = json.dumps(event_json["payload"], separators=(",", ":"), ensure_ascii=False)
            cursor.execute(
                "INSERT INTO run_events "
                "(event_id, run_id, lab_id, ts, type, seq, payload, source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (event.event_id, event.run_id, event.lab_id, event.ts, event.type, event.seq, sql_payload, event.source),
            )
        try:
            await self.bus.publish(
                subject,
                json.dumps(event_json, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            )
        except Exception as exc:
            raise EventFanoutError(event, exc) from exc
        return event

    def replay_events(self, identity: Identity, run_id: str, from_seq: int = 0) -> list[RunEvent]:
        if from_seq < 0:
            raise ValueError("from_seq must be non-negative")
        with self._access(identity, "runs:read") as cursor:
            self._run(cursor, identity, run_id, lock=False)
            cursor.execute(
                "SELECT event_id, run_id, lab_id, ts, type, seq, payload, source "
                "FROM run_events WHERE lab_id = %s AND run_id = %s AND seq > %s ORDER BY seq ASC",
                (identity.lab_id, run_id, from_seq),
            )
            return [self._event_from_row(row) for row in cursor.fetchall()]

    def subscribe(self, identity: Identity, run_id: str) -> AbstractAsyncContextManager[AsyncIterator[bytes]]:
        subject = _subject(identity.lab_id, run_id)
        with self._access(identity, "runs:read") as cursor:
            self._run(cursor, identity, run_id, lock=False)
        return self.bus.subscribe(subject)
