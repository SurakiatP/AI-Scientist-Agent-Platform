"""Executable PostgreSQL-backed REST Run worker for one Lab."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import nats
import psycopg
from minio import Minio

from scilab.api.inputs import InputUploadService
from scilab.approvals import ApprovalService, OPAClient
from scilab.events import EventService
from scilab.hermes import HermesClient
from scilab.identity import Identity
from scilab.infra.nats_events import NatsEventBus
from scilab.orchestration import ResearchCycle
from scilab.runs.executor import RunExecutor
from scilab.runs.model import RunState
from scilab.runs.service import RunService
from scilab.runs.worker import RunWorker

_LOG = logging.getLogger("scilab.runs.worker")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")
_REQUIRED_ENV = (
    "SCILAB_LAB_ID",
    "SCILAB_DATABASE_URL",
    "SCILAB_HERMES_API_KEY",
    "POD_NAMESPACE",
    "SCILAB_NATS_URL",
    "SCILAB_PI_PROVIDER",
    "SCILAB_REVIEWER_PROVIDER",
    "SCILAB_MINIO_URL",
    "SCILAB_MINIO_ACCESS_KEY",
    "SCILAB_MINIO_SECRET_KEY",
    "SCILAB_INPUT_BUCKET",
    "SCILAB_OPA_URL",
)


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _dns_label(value: str, name: str, *, limit: int) -> str:
    if len(value) > limit or _DNS_LABEL.fullmatch(value) is None:
        raise ValueError(f"{name} must be a DNS label")
    return value


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    lab_id: str
    database_url: str = field(repr=False)
    hermes_api_key: str = field(repr=False)
    namespace: str
    nats_url: str = field(repr=False)
    pi_provider: str
    reviewer_provider: str
    minio_url: str = field(repr=False)
    minio_access_key: str = field(repr=False)
    minio_secret_key: str = field(repr=False)
    input_bucket: str
    opa_url: str = field(repr=False)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None
    ) -> WorkerSettings:
        source = os.environ if environ is None else environ
        values = {name: _required(source, name) for name in _REQUIRED_ENV}
        lab_id = _dns_label(values["SCILAB_LAB_ID"], "SCILAB_LAB_ID", limit=40)
        namespace = _dns_label(values["POD_NAMESPACE"], "POD_NAMESPACE", limit=63)
        pi_provider = values["SCILAB_PI_PROVIDER"]
        reviewer_provider = values["SCILAB_REVIEWER_PROVIDER"]
        if pi_provider == reviewer_provider:
            raise ValueError("SCILAB_PI_PROVIDER and SCILAB_REVIEWER_PROVIDER must differ")
        endpoint = urlsplit(values["SCILAB_MINIO_URL"])
        if (
            endpoint.scheme not in {"http", "https"}
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("SCILAB_MINIO_URL must be a plain HTTP(S) origin")
        return cls(
            lab_id=lab_id,
            database_url=values["SCILAB_DATABASE_URL"],
            hermes_api_key=values["SCILAB_HERMES_API_KEY"],
            namespace=namespace,
            nats_url=values["SCILAB_NATS_URL"],
            pi_provider=pi_provider,
            reviewer_provider=reviewer_provider,
            minio_url=values["SCILAB_MINIO_URL"],
            minio_access_key=values["SCILAB_MINIO_ACCESS_KEY"],
            minio_secret_key=values["SCILAB_MINIO_SECRET_KEY"],
            input_bucket=values["SCILAB_INPUT_BUCKET"],
            opa_url=values["SCILAB_OPA_URL"],
        )

    @property
    def hermes_endpoint(self) -> str:
        return (
            f"http://scilab-{self.lab_id}-hermes.{self.namespace}"
            ".svc.cluster.local:8642"
        )


class _StreamingResponse:
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    async def aiter_lines(self) -> AsyncIterator[str]:
        async with self._stream as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line


class _HermesHTTPTransport:
    """Adapt HTTPX requests to HermesClient, streaming SSE without read timeout."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = kwargs.get("headers", {})
        if headers.get("Accept") == "text/event-stream":
            return _StreamingResponse(
                self._client.stream(
                    method,
                    url,
                    timeout=httpx.Timeout(connect=5, read=None, write=10, pool=5),
                    **kwargs,
                )
            )
        response = await self._client.request(method, url, **kwargs)
        response.raise_for_status()
        return response


def create_lab_cycle(settings: WorkerSettings, transport: Any) -> ResearchCycle:
    """Create a Hermes client and ResearchCycle bound to exactly one Lab."""
    hermes = HermesClient(
        {settings.lab_id: settings.hermes_endpoint},
        api_key=settings.hermes_api_key,
        session_id=secrets.token_urlsafe(24),
        session_key=secrets.token_urlsafe(24),
        transport=_HermesHTTPTransport(transport),
    )
    return ResearchCycle(
        hermes,
        settings.lab_id,
        settings.pi_provider,
        settings.reviewer_provider,
    )


async def poll_forever(
    executor: Any,
    identity: Identity,
    *,
    poll_interval: float = 1.0,
) -> None:
    """Poll durable claims forever; back off briefly on empty/error iterations."""
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")
    while True:
        try:
            run = await executor.execute_next(identity)
        except Exception:
            _LOG.exception("Run worker iteration failed for Lab %s", identity.lab_id)
            await asyncio.sleep(poll_interval)
            continue
        if run is None:
            await asyncio.sleep(poll_interval)


async def reconcile_pending_forever(
    inputs: InputUploadService,
    identity: Identity,
    *,
    interval: float = 60.0,
) -> None:
    """Retry bounded cleanup of this Lab's stale pending uploads."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    while True:
        try:
            await asyncio.to_thread(inputs.reconcile_pending,
                identity,
                before=datetime.now(UTC) - timedelta(minutes=5),
                limit=100,
            )
        except Exception:
            _LOG.exception("Pending input cleanup failed for Lab %s", identity.lab_id)
        await asyncio.sleep(interval)


async def reconcile_approvals_forever(
    approvals: ApprovalService,
    runs: RunService,
    client: HermesClient,
    events: EventService,
    identity: Identity,
    *,
    interval: float = 60.0,
) -> None:
    """Expire Lab approvals off the lease loop and stop their Hermes Runs."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    while True:
        try:
            expired = await asyncio.to_thread(approvals.expire_approvals, identity)
            for approval in expired:
                run = await asyncio.to_thread(runs.get, identity, approval.run_id)
                if run.state is not RunState.CANCELLED or run.reason != "approval_expired":
                    continue
                try:
                    await events.publish_event(
                        identity, run.id, "run.state",
                        {"from": "awaiting_approval", "to": "cancelled", "reason": "approval_expired"},
                        "run-service",
                    )
                except Exception:
                    _LOG.exception("Expiry state event failed for Run %s", run.id)
                if run.hermes_run_id:
                    try:
                        await client.stop(identity.lab_id, run.hermes_run_id)
                    except Exception:
                        _LOG.exception("Hermes stop failed for expired Run %s", run.id)
        except Exception:
            _LOG.exception("Approval expiry failed for Lab %s", identity.lab_id)
        await asyncio.sleep(interval)


async def run_worker(settings: WorkerSettings) -> None:
    connection = psycopg.connect(settings.database_url, autocommit=True)
    cleanup_connection = None
    approval_connection = None
    try:
        cleanup_connection = psycopg.connect(settings.database_url, autocommit=True)
        approval_connection = psycopg.connect(settings.database_url, autocommit=True)
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        nats_client = await nats.connect(settings.nats_url)
        try:
            async with httpx.AsyncClient(timeout=30.0) as transport:
                cycle = create_lab_cycle(settings, transport)
                events = EventService(connection, NatsEventBus(nats_client))
                expiry_events = EventService(approval_connection, NatsEventBus(nats_client))
                endpoint = urlsplit(settings.minio_url)
                storage = Minio(
                    endpoint.netloc,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=endpoint.scheme == "https",
                )
                inputs = InputUploadService(cleanup_connection, storage, bucket=settings.input_bucket)

                def cycle_for_lab(lab_id: str) -> ResearchCycle:
                    if lab_id != settings.lab_id:
                        raise ValueError("worker cannot execute a different Lab")
                    return cycle

                approvals = ApprovalService(connection, OPAClient(settings.opa_url), events)
                expiry = ApprovalService(approval_connection, OPAClient(settings.opa_url), expiry_events)
                executor = RunExecutor(RunWorker(connection), events, cycle_for_lab, approvals=approvals)
                identity = Identity(
                    settings.lab_id,
                    "service:run-worker",
                    frozenset({"runs:read", "runs:write", "lab:admin"}),
                )
                await asyncio.gather(
                    poll_forever(executor, identity),
                reconcile_pending_forever(inputs, identity),
                    reconcile_approvals_forever(expiry, RunService(approval_connection), cycle.client, expiry_events, identity),
                )
        finally:
            await nats_client.close()
    finally:
        if cleanup_connection is not None:
            cleanup_connection.close()
        if approval_connection is not None:
            approval_connection.close()
        connection.close()


def main() -> int:
    try:
        settings = WorkerSettings.from_environment()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    asyncio.run(run_worker(settings))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
