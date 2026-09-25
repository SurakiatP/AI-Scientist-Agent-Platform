from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import jwt
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from scilab.artifacts import Artifact
from scilab.identity import Identity


def test_container_loads_general_research_skill_pack() -> None:
    image = os.environ.get("SCILAB_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("set SCILAB_TEST_DOCKER_IMAGE to run the image smoke test")
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--entrypoint", "python", image,
            "-c", "from scilab.skill_catalog import get_skill_pack; assert 'paper-lookup' in get_skill_pack('general-research')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _config() -> dict[str, str]:
    return {
        "SCILAB_DATABASE_URL": "postgresql://runtime@db/scilab",
        "SCILAB_OIDC_ISSUER": "https://id.example/realms/scilab",
        "SCILAB_OIDC_AUDIENCE": "scilab-rest",
        "SCILAB_OIDC_JWKS_URI": "https://id.example/realms/scilab/certs",
        "SCILAB_MINIO_URL": "https://storage.example:9000",
        "SCILAB_MINIO_ACCESS_KEY": "test-access",
        "SCILAB_MINIO_SECRET_KEY": "test-secret",
        "SCILAB_INPUT_BUCKET": "inputs",
        "SCILAB_ARTIFACT_BUCKET": "artifacts",
        "SCILAB_NATS_URL": "nats://nats.example:4222",
        "SCILAB_OPA_URL": "http://opa.example:8181",
        "SCILAB_RUN_ADMISSION_PER_MINUTE": "7",
        "POD_NAMESPACE": "scilab",
    }


def test_runtime_fails_closed_without_database_configuration() -> None:
    with pytest.raises(ValueError, match="SCILAB_DATABASE_URL"):
        import_module("scilab.api.runtime").create_runtime_app({})


@pytest.mark.parametrize("missing", list(_config()))
def test_runtime_requires_every_external_setting(missing: str) -> None:
    config = _config()
    del config[missing]
    with pytest.raises(ValueError, match=missing):
        import_module("scilab.api.runtime").create_runtime_app(config)


def test_runtime_startup_rejects_database_connection_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = import_module("scilab.api.runtime")

    def connection_failed(*_args: object, **_kwargs: object) -> None:
        raise ConnectionError("database unavailable")

    monkeypatch.setattr("psycopg.connect", connection_failed)
    app = runtime.create_runtime_app(_config())
    with pytest.raises(ConnectionError, match="database unavailable"):
        with TestClient(app):
            pass


class _Database:
    def __init__(self) -> None:
        self.row: object = None
        self.closed = False

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def cursor(self) -> _Database:
        return self

    def __enter__(self) -> _Database:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        if "FROM lab_memberships" in sql:
            self.row = (
                {"subject": "subject-1", "lab_id": "lab-a", "role": "owner"}
                if params == ("subject-1", "lab-a") else None
            )
        elif sql.strip() == "SELECT 1":
            self.row = (1,)
        else:
            self.row = None

    def fetchone(self) -> object:
        return self.row

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def running_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    runtime = import_module("scilab.api.runtime")
    database = _Database()
    monkeypatch.setattr("psycopg.connect", lambda *_args, **_kwargs: database)

    class Storage:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def bucket_exists(self, bucket: str) -> bool:
            return bucket == "inputs"

    class S3:
        def head_bucket(self, **kwargs: str) -> None:
            assert kwargs == {"Bucket": "artifacts"}

        def close(self) -> None:
            pass

    class Nats:
        async def close(self) -> None:
            pass

    async def connect_nats(*_args: object, **_kwargs: object) -> Nats:
        return Nats()

    monkeypatch.setattr("minio.Minio", Storage)
    monkeypatch.setattr("boto3.client", lambda *_args, **_kwargs: S3())
    monkeypatch.setattr("nats.connect", connect_nats)
    monkeypatch.setattr(runtime.ssl, "create_default_context", lambda **_kwargs: object())
    account = tmp_path / "serviceaccount"
    account.mkdir()
    (account / "token").write_text("service-token")
    (account / "ca.crt").write_text("test-ca")
    monkeypatch.setattr(runtime, "SERVICE_ACCOUNT_DIRECTORY", account)

    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class StaticJwks:
        def __init__(self, _uri: str) -> None:
            pass

        def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
            return SimpleNamespace(key=signing_key.public_key())

    monkeypatch.setattr("scilab.api.oidc.jwt.PyJWKClient", StaticJwks)
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "subject-1",
            "lab_id": "lab-a",
            "iss": _config()["SCILAB_OIDC_ISSUER"],
            "aud": _config()["SCILAB_OIDC_AUDIENCE"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        signing_key,
        algorithm="RS256",
    )
    with TestClient(runtime.create_runtime_app(_config())) as client:
        yield client, token
    assert database.closed


def test_runtime_serves_authenticated_identity_and_real_skill_catalog(running_runtime: tuple[TestClient, str]) -> None:
    client, token = running_runtime
    headers = {"Authorization": f"Bearer {token}"}
    identity = client.get("/v1/me", headers=headers)
    assert identity.status_code == 200
    assert identity.json()["principal"] == "user:subject-1"
    skills = client.get("/v1/labs/lab-a/skills", headers=headers)
    assert skills.status_code == 200
    assert skills.json()[0]["pack"] == "general-research"
    assert skills.json()[0]["skills"]


def test_runtime_rejects_unauthenticated_and_cross_lab_requests(running_runtime: tuple[TestClient, str]) -> None:
    client, token = running_runtime
    assert client.get("/v1/me").status_code == 401
    denied = client.get("/v1/labs/lab-b/skills", headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403


def test_ask_sends_the_lab_secret_to_its_own_hermes_service() -> None:
    runtime = import_module("scilab.api.runtime")
    content = b"Response rate 42 percent."
    artifact = Artifact(
        id="doc-a",
        run_id="run-a",
        lab_id="lab-a",
        kind="document",
        uri="s3://lab-a/run-a/doc-a",
        sha256=hashlib.sha256(content).hexdigest(),
        bytes=len(content),
        produced_by_step=1,
        metadata={"media_type": "text/markdown"},
        created_at=datetime.now(UTC),
    )

    class Sources:
        def list_knowledge_sources(self, identity: Identity, *, limit: int) -> list[Artifact]:
            assert identity.lab_id == "lab-a" and limit > 0
            return [artifact]

        def read_bytes(self, identity: Identity, artifact_id: str) -> bytes:
            assert identity.lab_id == "lab-a" and artifact_id == "doc-a"
            return content

    class LabSecret:
        def resolve(self, lab_id: str) -> tuple[str, str]:
            assert lab_id == "lab-a"
            return "http://scilab-lab-a-hermes.scilab.svc.cluster.local:8642", "lab-a-key"

    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "42 percent."}}]})

    async def ask() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as transport:
            service = runtime._Ask(Sources(), LabSecret(), transport)
            identity = Identity("lab-a", "user:subject-1", frozenset({"artifacts:read"}))
            return await service.ask(identity, {"question": "What was response rate?"})

    result = asyncio.run(ask())
    assert result["answer"] == "42 percent."
    assert requests[0].url.host == "scilab-lab-a-hermes.scilab.svc.cluster.local"
    assert requests[0].headers["authorization"] == "Bearer lab-a-key"


def test_runtime_wires_approval_delivery(running_runtime: tuple[TestClient, str]) -> None:
    from scilab.api.hermes_approval import HermesApprovalDelivery

    client, _ = running_runtime
    route = next(route for route in client.app.routes if route.path == "/v1/runs/{id}/approvals/{approval_id}")
    closure = dict(zip(route.endpoint.__code__.co_freevars, (cell.cell_contents for cell in route.endpoint.__closure__)))
    assert isinstance(closure["services"].approvals, HermesApprovalDelivery)
