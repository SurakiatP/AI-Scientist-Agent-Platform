from __future__ import annotations

import base64
import copy
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from scilab.api.inputs import InputUploadService, UploadedInputNotFound
from scilab.identity import Identity


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.results: list[tuple[Any, ...]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        statement = " ".join(sql.lower().split())
        self.connection.statements.append((statement, params))
        if statement.startswith("select set_config"):
            if params[0] == "scilab.current_lab_id":
                self.connection.current_lab_id = params[1]
        elif statement.startswith("insert into uploaded_inputs"):
            if self.connection.insert_error is not None:
                raise self.connection.insert_error
            row = dict(
                zip(
                    (
                        "id",
                        "lab_id",
                        "name",
                        "media_type",
                        "bucket_name",
                        "object_key",
                        "sha256",
                        "bytes",
                        "created_at",
                    ),
                    params,
                    strict=True,
                )
            )
            row["status"] = "pending"
            self.connection.rows.append(row)
        elif statement.startswith("update uploaded_inputs set status = 'ready'"):
            lab_id, input_id = params
            self.results = []
            for row in self.connection.rows:
                if row["lab_id"] == lab_id and row["id"] == input_id and row["status"] == "pending":
                    row["status"] = "ready"
                    self.results = [(input_id,)]
        elif statement.startswith("delete from uploaded_inputs"):
            lab_id, input_id = params
            self.connection.rows[:] = [
                row for row in self.connection.rows
                if not (row["lab_id"] == lab_id and row["id"] == input_id and row["status"] == "pending")
            ]
        elif statement.startswith("select id, bucket_name, object_key from uploaded_inputs"):
            lab_id, before, limit = params
            self.results = [
                (row["id"], row["bucket_name"], row["object_key"])
                for row in self.connection.rows
                if row["lab_id"] == lab_id
                and row["status"] == "pending"
                and row["created_at"] < before
            ][:limit]
        elif statement.startswith("select id from uploaded_inputs") and "for update" in statement:
            lab_id, input_id = params
            self.results = [
                (row["id"],) for row in self.connection.rows
                if row["lab_id"] == lab_id and row["id"] == input_id and row["status"] == "pending"
            ]
        elif statement.startswith("select id from uploaded_inputs"):
            lab_id, ids = params
            self.results = [
                (row["id"],)
                for row in self.connection.rows
                if row["lab_id"] == lab_id and row["id"] in ids and row.get("status") == "ready"
            ]
        else:
            raise AssertionError(f"unexpected SQL: {statement}")

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.results

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.results.pop(0) if self.results else None


class FakeConnection:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.current_lab_id: str | None = None
        self.insert_error: Exception | None = None
        self.commit_error: Exception | None = None

    @contextmanager
    def transaction(self):
        old_rows = copy.deepcopy(self.rows)
        try:
            yield
            if self.commit_error is not None:
                raise self.commit_error
        except Exception:
            self.rows[:] = old_rows
            raise

    @contextmanager
    def cursor(self):
        yield FakeCursor(self)


class FakeMinio:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.put_calls: list[tuple[str, str, int, str]] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.put_error: Exception | None = None

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: Any,
        length: int,
        content_type: str = "application/octet-stream",
        **_: Any,
    ) -> None:
        body = data.read()
        self.put_calls.append((bucket_name, object_name, length, content_type))
        self.objects[(bucket_name, object_name)] = (body, content_type)
        if self.put_error is not None:
            raise self.put_error

    def remove_object(self, bucket_name: str, object_name: str) -> None:
        self.remove_calls.append((bucket_name, object_name))
        if getattr(self, "remove_error", None) is not None:
            raise self.remove_error
        self.objects.pop((bucket_name, object_name), None)


def test_create_persists_lab_upload_and_returns_web_compatible_artifact_id() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))
    content = b"\x00data"
    service = InputUploadService(
        connection,
        storage,
        bucket="lab-inputs",
        max_upload_bytes=len(content),
        clock=lambda: datetime(2026, 9, 24, tzinfo=UTC),
        id_factory=lambda: "input-1",
    )

    result = service.create(
        identity,
        {
            "name": "paper.bin",
            "media_type": "application/octet-stream",
            "data_base64": base64.b64encode(content).decode("ascii"),
        },
    )

    assert result == {"artifact_id": "input-1"}
    assert storage.objects[("lab-inputs", "labs/lab-a/inputs/input-1")] == (
        content,
        "application/octet-stream",
    )
    assert connection.rows[0]["lab_id"] == "lab-a"
    assert connection.rows[0]["name"] == "paper.bin"
    assert connection.rows[0]["bytes"] == len(content)
    assert connection.current_lab_id == "lab-a"


def test_upload_and_reconciliation_lock_pending_row_during_object_io() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    service = InputUploadService(connection, storage, bucket="inputs", id_factory=lambda: "input-1")
    writer = Identity("lab-a", "user:writer", frozenset({"runs:write"}))
    service.create(writer, {"name": "paper.txt", "media_type": "text/plain", "data_base64": "cGFwZXI="})
    assert any("for update" in sql and "status = 'pending'" in sql for sql, _ in connection.statements)
    owner = Identity("lab-a", "user:owner", frozenset({"lab:admin"}))
    service.reconcile_pending(owner, before=datetime.max.replace(tzinfo=UTC))
    assert any("for update skip locked" in sql for sql, _ in connection.statements)
    assert storage.objects[("inputs", "labs/lab-a/inputs/input-1")][0] == b"paper"


def test_create_rejects_malformed_base64_before_writing() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    service = InputUploadService(connection, storage, bucket="lab-inputs")
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": "%%%",
            },
        )

    assert error.value.status_code == 422
    assert connection.rows == []
    assert storage.objects == {}


def test_create_rejects_file_over_configured_limit_before_writing() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", max_upload_bytes=4
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(b"large").decode("ascii"),
            },
        )

    assert error.value.status_code == 413
    assert connection.rows == []
    assert storage.objects == {}
    assert storage.put_calls == []


def test_create_rejects_encoded_payload_too_large_before_base64_decode() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", max_upload_bytes=4
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": "!" * 9,
            },
        )

    assert error.value.status_code == 413
    assert connection.rows == []
    assert storage.put_calls == []


def test_storage_failure_does_not_leave_uploaded_input_metadata_or_object() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    storage.put_error = RuntimeError("private MinIO response")
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", id_factory=lambda: "input-1"
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(b"paper").decode("ascii"),
            },
        )

    assert error.value.status_code == 503
    assert "private MinIO response" not in str(error.value.detail)
    assert connection.rows == []
    assert storage.objects == {}
    assert storage.remove_calls == [("lab-inputs", "labs/lab-a/inputs/input-1")]


def test_database_insert_failure_never_uploads_minio_object() -> None:
    connection = FakeConnection()
    connection.insert_error = RuntimeError("private database response")
    storage = FakeMinio()
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", id_factory=lambda: "input-1"
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(b"paper").decode("ascii"),
            },
        )

    assert error.value.status_code == 503
    assert "private database response" not in str(error.value.detail)
    assert connection.rows == []
    assert storage.objects == {}
    assert storage.put_calls == []
    assert storage.remove_calls == []


def test_pending_record_commit_failure_never_uploads_minio_object() -> None:
    connection = FakeConnection()
    connection.commit_error = RuntimeError("private database response")
    storage = FakeMinio()
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", id_factory=lambda: "input-1"
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(b"paper").decode("ascii"),
            },
        )

    assert error.value.status_code == 503
    assert "private database response" not in str(error.value.detail)
    assert connection.rows == []
    assert storage.objects == {}
    assert storage.put_calls == []
    assert storage.remove_calls == []


def test_failed_compensation_retains_pending_record_for_recovery() -> None:
    connection = FakeConnection()
    storage = FakeMinio()
    storage.put_error = RuntimeError("put timed out after writing")
    storage.remove_error = RuntimeError("delete unavailable")
    service = InputUploadService(
        connection, storage, bucket="lab-inputs", id_factory=lambda: "input-1"
    )
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(HTTPException) as error:
        service.create(
            identity,
            {
                "name": "paper.txt",
                "media_type": "text/plain",
                "data_base64": base64.b64encode(b"paper").decode("ascii"),
            },
        )

    assert error.value.status_code == 503
    assert "pending recovery" in error.value.detail
    assert connection.rows[0]["status"] == "pending"
    with pytest.raises(UploadedInputNotFound):
        service.validate_refs(identity, ["input-1"])

    storage.remove_error = None
    owner = Identity("lab-a", "user:owner", frozenset({"lab:admin"}))
    assert service.reconcile_pending(owner, before=datetime.max.replace(tzinfo=UTC)) == 1
    assert connection.rows == []
    assert storage.objects == {}


def test_reconcile_pending_handles_crash_before_put_without_cross_lab_cleanup() -> None:
    connection = FakeConnection()
    now = datetime(2026, 9, 24, tzinfo=UTC)
    connection.rows.extend([
        {"id": "pending-a", "lab_id": "lab-a", "status": "pending", "bucket_name": "inputs", "object_key": "labs/lab-a/inputs/pending-a", "created_at": now},
        {"id": "ready-a", "lab_id": "lab-a", "status": "ready", "bucket_name": "inputs", "object_key": "labs/lab-a/inputs/ready-a", "created_at": now},
        {"id": "pending-b", "lab_id": "lab-b", "status": "pending", "bucket_name": "inputs", "object_key": "labs/lab-b/inputs/pending-b", "created_at": now},
    ])
    storage = FakeMinio()
    service = InputUploadService(connection, storage, bucket="inputs")
    owner = Identity("lab-a", "user:owner", frozenset({"lab:admin"}))

    assert service.reconcile_pending(owner, before=datetime.max.replace(tzinfo=UTC)) == 1
    assert [row["id"] for row in connection.rows] == ["ready-a", "pending-b"]
    assert storage.remove_calls == [("inputs", "labs/lab-a/inputs/pending-a")]


def test_validate_refs_returns_existing_ids_in_input_order() -> None:
    connection = FakeConnection()
    connection.rows.extend(
        [
            {"id": "input-1", "lab_id": "lab-a", "status": "ready"},
            {"id": "input-2", "lab_id": "lab-a", "status": "ready"},
        ]
    )
    service = InputUploadService(connection, FakeMinio(), bucket="lab-inputs")
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    assert service.validate_refs(identity, ["input-2", "input-1"]) == [
        "input-2",
        "input-1",
    ]
    assert connection.current_lab_id == "lab-a"


def test_validate_refs_hides_cross_lab_uploads_as_not_found() -> None:
    connection = FakeConnection()
    connection.rows.append({"id": "input-b", "lab_id": "lab-b"})
    service = InputUploadService(connection, FakeMinio(), bucket="lab-inputs")
    identity = Identity("lab-a", "user:alice", frozenset({"runs:write"}))

    with pytest.raises(UploadedInputNotFound, match="uploaded input not found"):
        service.validate_refs(identity, ["input-b"])

    assert connection.current_lab_id == "lab-a"
