"""Production REST composition for the Control Plane."""

from __future__ import annotations

import asyncio
import os
import secrets
import ssl
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import boto3
import httpx
import minio
import nats
import psycopg
from fastapi import FastAPI, Request

from scilab.api.admission import PostgresRunAdmission
from scilab.api.credentials_admin import A2APeerAdminService, APIKeyAdminService
from scilab.api.hermes_credential import HermesSecretResolver
from scilab.api.hermes_approval import HermesApprovalDelivery
from scilab.api.inputs import InputUploadService
from scilab.api.lab_admin import LabAdminService
from scilab.api.oidc import OIDCIdentityResolver
from scilab.api.rest import create_app
from scilab.api.run_adapters import RunAdmissionAdapter, RunSearchAdapter, RunSubmissionAdapter
from scilab.api.usage_period import UsagePeriodService
from scilab.approvals import ApprovalService, OPAClient
from scilab.artifacts import ArtifactService
from scilab.audit import AuditService
from scilab.events import EventService
from scilab.hermes import HermesClient
from scilab.identity import Identity
from scilab.infra.nats_events import NatsEventBus
from scilab.infra.s3_artifacts import S3ArtifactStorage
from scilab.lab_knowledge import LabKnowledgeService
from scilab.metering import MeteringService
from scilab.runs.service import RunService
from scilab.skill_catalog import get_skill_pack
from scilab.tenancy import require_scope


SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_REQUIRED = (
    "SCILAB_DATABASE_URL",
    "SCILAB_OIDC_ISSUER",
    "SCILAB_OIDC_AUDIENCE",
    "SCILAB_OIDC_JWKS_URI",
    "SCILAB_MINIO_URL",
    "SCILAB_MINIO_ACCESS_KEY",
    "SCILAB_MINIO_SECRET_KEY",
    "SCILAB_INPUT_BUCKET",
    "SCILAB_ARTIFACT_BUCKET",
    "SCILAB_NATS_URL",
    "SCILAB_OPA_URL",
    "SCILAB_RUN_ADMISSION_PER_MINUTE",
    "POD_NAMESPACE",
)


class _Skills:
    def list(self, identity: Identity, *, filter: str | None = None) -> list[dict[str, object]]:
        require_scope(identity, "runs:read")
        skills = get_skill_pack("general-research")
        if filter is not None:
            skills = [skill for skill in skills if filter.casefold() in skill.casefold()]
        return [{"pack": "general-research", "skills": skills}]


class _Ask:
    def __init__(
        self, artifacts: ArtifactService, resolver: HermesSecretResolver, transport: httpx.AsyncClient
    ) -> None:
        self.artifacts = artifacts
        self.resolver = resolver
        self.transport = transport

    async def ask(self, identity: Identity, body: Mapping[str, str]) -> dict[str, object]:
        endpoint, api_key = await asyncio.to_thread(self.resolver.resolve, identity.lab_id)
        hermes = HermesClient(
            {identity.lab_id: endpoint},
            api_key=api_key,
            session_id=secrets.token_urlsafe(24),
            session_key=secrets.token_urlsafe(24),
            transport=self.transport,
        )
        return await LabKnowledgeService(
            self.artifacts, hermes, model="sci-pi-frontier"
        ).ask(identity, body["question"])


def create_runtime_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    config = dict(os.environ if environ is None else environ)
    for name in _REQUIRED:
        if not isinstance(config.get(name), str) or not config[name].strip():
            raise ValueError(f"{name} is required")
    endpoint = urlsplit(config["SCILAB_MINIO_URL"])
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

    services = SimpleNamespace()
    oidc: OIDCIdentityResolver | None = None

    def resolve_identity(request: Request) -> Identity:
        if oidc is None:
            raise RuntimeError("REST runtime has not started")
        return oidc(request)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal oidc
        async with AsyncExitStack() as stack:
            # ponytail: one connection serializes synchronous REST DB work; use a pool if DB calls move to threads.
            connection = psycopg.connect(config["SCILAB_DATABASE_URL"], autocommit=True)
            stack.callback(connection.close)
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")

            storage = minio.Minio(
                endpoint.netloc,
                access_key=config["SCILAB_MINIO_ACCESS_KEY"],
                secret_key=config["SCILAB_MINIO_SECRET_KEY"],
                secure=endpoint.scheme == "https",
            )
            if not storage.bucket_exists(config["SCILAB_INPUT_BUCKET"]):
                raise RuntimeError("SCILAB_INPUT_BUCKET does not exist")
            s3 = boto3.client(
                "s3",
                endpoint_url=config["SCILAB_MINIO_URL"],
                aws_access_key_id=config["SCILAB_MINIO_ACCESS_KEY"],
                aws_secret_access_key=config["SCILAB_MINIO_SECRET_KEY"],
                region_name="us-east-1",
            )
            stack.callback(s3.close)
            s3.head_bucket(Bucket=config["SCILAB_ARTIFACT_BUCKET"])

            nats_client = await nats.connect(config["SCILAB_NATS_URL"])
            stack.push_async_callback(nats_client.close)
            transport = await stack.enter_async_context(httpx.AsyncClient(timeout=30.0))
            token = (SERVICE_ACCOUNT_DIRECTORY / "token").read_text().strip()
            ca = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT_DIRECTORY / "ca.crt"))
            hermes_secrets = HermesSecretResolver(
                namespace=config["POD_NAMESPACE"], bearer_token=token, ssl_context=ca
            )

            runs = RunService(connection)
            events = EventService(connection, NatsEventBus(nats_client))
            artifacts = ArtifactService(
                connection, S3ArtifactStorage(s3, bucket=config["SCILAB_ARTIFACT_BUCKET"])
            )
            inputs = InputUploadService(connection, storage, bucket=config["SCILAB_INPUT_BUCKET"])
            metering = MeteringService(
                connection,
                event_service=events,
                run_service=runs,
                audit_service=AuditService(connection),
            )
            admission = PostgresRunAdmission.from_environment(connection, config)
            services.runs = runs
            services.events = events
            services.artifacts = artifacts
            services.inputs = inputs
            services.admission = RunAdmissionAdapter(admission.admit)
            services.run_submission = RunSubmissionAdapter(
                runs, input_resolver=inputs.validate_refs
            )
            services.run_search = RunSearchAdapter(runs)
            approval_service = ApprovalService(connection, OPAClient(config["SCILAB_OPA_URL"]), events)
            services.approvals = HermesApprovalDelivery(approval_service, hermes_secrets, transport, events)
            services.lab_admin = LabAdminService(connection)
            services.api_keys = APIKeyAdminService(connection)
            services.peers = A2APeerAdminService(connection)
            services.ask = _Ask(artifacts, hermes_secrets, transport)
            services.skills = _Skills()
            services.usage = UsagePeriodService(metering)
            oidc = OIDCIdentityResolver(
                issuer=config["SCILAB_OIDC_ISSUER"],
                audience=config["SCILAB_OIDC_AUDIENCE"],
                jwks_uri=config["SCILAB_OIDC_JWKS_URI"],
                connection=connection,
            )
            try:
                yield
            finally:
                oidc = None
                vars(services).clear()

    app = create_app(services, resolve_identity)
    app.router.lifespan_context = lifespan
    return app
