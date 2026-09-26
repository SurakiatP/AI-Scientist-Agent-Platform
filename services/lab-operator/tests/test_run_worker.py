from __future__ import annotations

import pytest

pytest.importorskip("lab_operator")
from lab_operator.resources import build_resources, build_run_worker


IMAGE = "registry.example/scilab-api@sha256:" + "c" * 64
HERMES_IMAGE = "registry.example/hermes@sha256:" + "a" * 64
SKILLS_IMAGE = "registry.example/skills@sha256:" + "b" * 64
SANDBOX_IMAGE = "registry.example/sandbox@sha256:" + "d" * 64
CONFIG_SHA256 = "e" * 64

WORKER_WIRING = dict(
    hermes_image=HERMES_IMAGE,
    skills_image=SKILLS_IMAGE,
    hermes_config_sha256=CONFIG_SHA256,
    sandbox_image=SANDBOX_IMAGE,
    model_prices_thb='{"sci-pi-frontier": {"in": 100.0, "out": 200.0}}',
)


def test_worker_is_tenant_scoped_and_uses_referenced_secrets() -> None:
    worker = build_run_worker(
        lab_id="alpha",
        namespace="research",
        uid="uid-alpha",
        image=IMAGE,
        database_secret="scilab-postgres",
        nats_secret="scilab-nats",
        pi_provider="pi-deployment-choice",
        reviewer_provider="reviewer-deployment-choice",
        minio_secret="scilab-minio",
        resources={"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "1", "memory": "1Gi"}},
        release="scilab",
        **WORKER_WIRING,
    )

    assert worker["kind"] == "Deployment"
    assert worker["metadata"]["name"] == "scilab-alpha-run-worker"
    pod = worker["spec"]["template"]
    assert pod["metadata"]["labels"]["scilab.ai/lab-id"] == "alpha"
    assert pod["metadata"]["labels"]["scilab.ai/platform-release"] == "scilab"
    assert pod["metadata"]["labels"]["scilab.ai/component"] == "run-service"
    container = pod["spec"]["containers"][0]
    assert pod["spec"]["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["resources"]["limits"]["memory"] == "1Gi"
    assert container["envFrom"] == [{"secretRef": {"name": "scilab-minio"}}]
    assert container["image"] == IMAGE
    assert container["command"] == ["python", "-m", "scilab.runs.worker_main"]
    env = {entry["name"]: entry for entry in container["env"]}
    assert env["SCILAB_LAB_ID"]["value"] == "alpha"
    assert env["SCILAB_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "scilab-postgres",
        "key": "SCILAB_DATABASE_URL",
    }
    assert env["SCILAB_HERMES_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "scilab-alpha-hermes-api",
        "key": "api_key",
    }
    assert env["POD_NAMESPACE"]["valueFrom"]["fieldRef"] == {
        "fieldPath": "metadata.namespace"
    }
    assert env["SCILAB_NATS_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "scilab-nats", "key": "SCILAB_NATS_URL"
    }
    assert env["SCILAB_PI_PROVIDER"]["value"] == "pi-deployment-choice"
    assert env["SCILAB_REVIEWER_PROVIDER"]["value"] == "reviewer-deployment-choice"
    assert env["SCILAB_HERMES_IMAGE"]["value"] == HERMES_IMAGE
    assert env["SCILAB_SKILLS_IMAGE"]["value"] == SKILLS_IMAGE
    assert env["SCILAB_HERMES_CONFIG_SHA256"]["value"] == CONFIG_SHA256
    assert env["SCILAB_SANDBOX_IMAGE"]["value"] == SANDBOX_IMAGE
    assert env["SCILAB_MODEL_PRICES_THB"]["value"] == WORKER_WIRING["model_prices_thb"]


def test_worker_rejects_unpinned_image_and_invalid_secret_name() -> None:
    with pytest.raises(ValueError, match="digest"):
        build_run_worker("alpha", "research", "uid-alpha", "scilab:latest", "db", "nats", "pi", "reviewer", "minio", {}, "scilab", **WORKER_WIRING)
    with pytest.raises(ValueError, match="secret"):
        build_run_worker("alpha", "research", "uid-alpha", IMAGE, "bad/name", "nats", "pi", "reviewer", "minio", {}, "scilab", **WORKER_WIRING)
    with pytest.raises(ValueError, match="Reviewer provider"):
        build_run_worker("alpha", "research", "uid-alpha", IMAGE, "db", "nats", "same", "same", "minio", {}, "scilab", **WORKER_WIRING)


def _set_hermes_egress_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCILAB_API_PORT", "8000")
    monkeypatch.setenv("SCILAB_OPENSANDBOX_PORT", "8080")
    monkeypatch.setenv("SCILAB_EXTERNAL_CIDRS", "")


def test_reconcile_applies_per_lab_worker_when_chart_configures_it(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    from lab_operator import handlers
    from test_reconcile import lab_spec

    _set_hermes_egress_env(monkeypatch)
    monkeypatch.setenv("SCILAB_RUN_WORKER_IMAGE", IMAGE)
    monkeypatch.setenv("SCILAB_DATABASE_SECRET_NAME", "scilab-postgres")
    monkeypatch.setenv("SCILAB_NATS_SECRET_NAME", "scilab-nats")
    monkeypatch.setenv("SCILAB_MINIO_SECRET_NAME", "scilab-minio")
    monkeypatch.setenv("SCILAB_RELEASE_NAME", "scilab")
    monkeypatch.setenv("SCILAB_PI_PROVIDER", "pi-deployment-choice")
    monkeypatch.setenv("SCILAB_REVIEWER_PROVIDER", "reviewer-deployment-choice")
    monkeypatch.setenv("SCILAB_SANDBOX_IMAGE", SANDBOX_IMAGE)
    monkeypatch.setenv("SCILAB_MODEL_PRICES_THB", "{}")
    applied: list[tuple[dict, ...]] = []
    monkeypatch.setattr(handlers, "apply_resources", lambda resources: applied.append(resources))

    handlers.reconcile(spec=lab_spec(), name="alpha", namespace="research", uid="uid-alpha")

    assert len(applied) == 1
    resources = applied[0]
    assert [resource["metadata"]["name"] for resource in resources][-1] == "scilab-alpha-run-worker"
    config_map = next(r for r in resources if r["kind"] == "ConfigMap")
    expected_sha = hashlib.sha256(config_map["data"]["config.yaml"].encode("utf-8")).hexdigest()
    worker = resources[-1]
    env = {entry["name"]: entry.get("value") for entry in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCILAB_HERMES_CONFIG_SHA256"] == expected_sha
    assert env["SCILAB_HERMES_IMAGE"] == lab_spec()["hermesImage"]
    assert env["SCILAB_SKILLS_IMAGE"] == lab_spec()["skillsImage"]
    assert env["SCILAB_SANDBOX_IMAGE"] == SANDBOX_IMAGE
    assert env["SCILAB_MODEL_PRICES_THB"] == "{}"


def test_reconcile_rejects_half_configured_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    from lab_operator import handlers
    from test_reconcile import lab_spec

    _set_hermes_egress_env(monkeypatch)
    monkeypatch.setenv("SCILAB_RUN_WORKER_IMAGE", IMAGE)
    monkeypatch.delenv("SCILAB_DATABASE_SECRET_NAME", raising=False)
    with pytest.raises(ValueError, match="SCILAB_DATABASE_SECRET_NAME"):
        handlers.reconcile(spec=lab_spec(), name="alpha", namespace="research", uid="uid-alpha")


def test_hermes_ingress_only_accepts_its_own_lab_worker() -> None:
    from test_reconcile import lab_spec

    resources = build_resources(
        name="alpha", namespace="research", uid="uid-alpha", spec=lab_spec(),
        api_port=8000, opensandbox_port=8080, external_cidrs=(),
    )
    policy = next(resource for resource in resources if resource["kind"] == "NetworkPolicy")
    ingress = policy["spec"]["ingress"][0]["from"]
    assert {"podSelector": {"matchLabels": {
        "scilab.ai/component": "run-service", "scilab.ai/lab-id": "alpha"
    }}} in ingress


def test_hermes_and_worker_reference_same_per_lab_api_key() -> None:
    from test_reconcile import lab_spec

    resources = build_resources(
        name="alpha", namespace="research", uid="uid-alpha", spec=lab_spec(),
        api_port=8000, opensandbox_port=8080, external_cidrs=(),
    )
    hermes = next(resource for resource in resources if resource["kind"] == "Deployment")
    env = {entry["name"]: entry for entry in hermes["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["API_SERVER_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "scilab-alpha-hermes-api", "key": "api_key"
    }
