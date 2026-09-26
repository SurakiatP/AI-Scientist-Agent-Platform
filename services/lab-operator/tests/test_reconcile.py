from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lab_operator")


ROOT = Path(__file__).parents[3]
CRD_PATH = ROOT / "deploy/helm/scilab/crds/lab.yaml"


def lab_spec(lab_id: str = "alpha") -> dict[str, Any]:
    return {
        "labId": lab_id,
        "hermesImage": "registry.example/hermes@sha256:" + "a" * 64,
        "skillsImage": "registry.example/skills@sha256:" + "b" * 64,
        "pvcSize": "20Gi",
        "resources": {
            "requests": {"cpu": "500m", "memory": "1Gi"},
            "limits": {"cpu": "2", "memory": "4Gi"},
        },
        "secretRefs": [{"name": f"{lab_id}-hermes-secrets"}],
    }


def resources_for(
    lab_id: str = "alpha",
    *,
    api_port: int = 8000,
    opensandbox_port: int = 8080,
    external_cidrs: tuple[str, ...] = (),
) -> tuple[dict[str, Any], ...]:
    from lab_operator.resources import build_resources

    return build_resources(
        name=lab_id,
        namespace="research",
        uid=f"uid-{lab_id}",
        spec=lab_spec(lab_id),
        api_port=api_port,
        opensandbox_port=opensandbox_port,
        external_cidrs=external_cidrs,
    )


class FakeApi:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, method: str):
        def call(*args: Any, **kwargs: Any) -> None:
            self.calls.append((method, args, kwargs))

        return call


def by_kind(resources: tuple[dict[str, Any], ...], kind: str) -> dict[str, Any]:
    return next(resource for resource in resources if resource["kind"] == kind)


def test_crd_is_structural_and_declares_tor_validation() -> None:
    yaml = pytest.importorskip("yaml")
    crd = yaml.safe_load(CRD_PATH.read_text())
    schema = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    spec = schema["properties"]["spec"]

    assert crd["apiVersion"] == "apiextensions.k8s.io/v1"
    assert crd["spec"]["scope"] == "Namespaced"
    assert set(spec["required"]) == {
        "labId",
        "hermesImage",
        "skillsImage",
        "pvcSize",
        "resources",
        "secretRefs",
    }
    assert spec["x-kubernetes-preserve-unknown-fields"] is False
    assert schema["properties"]["metadata"]["x-kubernetes-preserve-unknown-fields"] is False
    assert "self.metadata.name == self.spec.labId" in schema["x-kubernetes-validations"][0]["rule"]
    validations = spec["x-kubernetes-validations"]
    assert any("self.labId == oldSelf.labId" in item["rule"] for item in validations)
    assert any("self.hermesImage == oldSelf.hermesImage" in item["rule"] for item in validations)
    assert any("self.skillsImage == oldSelf.skillsImage" in item["rule"] for item in validations)
    assert "@sha256:" in spec["properties"]["hermesImage"]["pattern"]
    assert "@sha256:" in spec["properties"]["skillsImage"]["pattern"]


def test_builder_is_deterministic_and_emits_exact_owned_children() -> None:
    first = resources_for()
    second = resources_for()

    assert first == second
    assert [resource["kind"] for resource in first] == [
        "PersistentVolumeClaim",
        "ConfigMap",
        "Deployment",
        "Service",
        "NetworkPolicy",
        "NetworkPolicy",
    ]
    assert [resource["metadata"]["name"] for resource in first] == [
        "scilab-alpha-hermes-home",
        "scilab-alpha-hermes-config",
        "scilab-alpha-hermes",
        "scilab-alpha-hermes",
        "scilab-alpha-hermes-ingress",
        "scilab-alpha-hermes-egress",
    ]
    labels = {
        "app.kubernetes.io/name": "hermes",
        "app.kubernetes.io/instance": "scilab-alpha",
        "app.kubernetes.io/managed-by": "scilab-lab-operator",
        "scilab.ai/component": "hermes",
        "scilab.ai/lab-id": "alpha",
    }
    assert all(resource["metadata"]["labels"] == labels for resource in first)
    owner = {
        "apiVersion": "scilab.ai/v1alpha1",
        "kind": "Lab",
        "name": "alpha",
        "uid": "uid-alpha",
        "controller": True,
        "blockOwnerDeletion": True,
    }
    assert all(resource["metadata"]["ownerReferences"] == [owner] for resource in first)


def test_two_labs_have_disjoint_names_and_identity() -> None:
    alpha = resources_for("alpha")
    beta = resources_for("beta")

    assert {resource["metadata"]["name"] for resource in alpha}.isdisjoint(
        {resource["metadata"]["name"] for resource in beta}
    )
    assert {
        resource["metadata"]["labels"]["scilab.ai/lab-id"] for resource in alpha
    } == {"alpha"}
    assert {
        resource["metadata"]["ownerReferences"][0]["name"] for resource in beta
    } == {"beta"}


def test_hermes_approval_config_is_lab_owned_and_mounted_read_only() -> None:
    resources = resources_for()
    config = by_kind(resources, "ConfigMap")
    deployment = by_kind(resources, "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    hermes = pod["containers"][0]

    assert config["metadata"]["name"] == "scilab-alpha-hermes-config"
    assert config["metadata"]["ownerReferences"][0]["name"] == "alpha"
    assert config["data"]["config.yaml"] == "approvals:\n  mode: manual\n  timeout: 86400\n"
    assert {"name": "hermes-config", "configMap": {"name": config["metadata"]["name"]}} in pod["volumes"]
    assert {"name": "hermes-config", "mountPath": "/var/lib/hermes/config.yaml", "subPath": "config.yaml", "readOnly": True} in hermes["volumeMounts"]
    assert {"name": "hermes-config", "mountPath": "/var/lib/hermes/config.yaml", "subPath": "config.yaml", "readOnly": True} not in pod["initContainers"][0]["volumeMounts"]


def _worker_for(**overrides: Any) -> dict[str, Any]:
    from lab_operator.resources import build_run_worker

    kwargs: dict[str, Any] = {
        "lab_id": "alpha",
        "namespace": "research",
        "uid": "uid-alpha",
        "image": "registry.example/worker@sha256:" + "c" * 64,
        "database_secret": "scilab-postgres",
        "nats_secret": "scilab-nats",
        "pi_provider": "pi",
        "reviewer_provider": "reviewer",
        "minio_secret": "scilab-minio",
        "resources": lab_spec()["resources"],
        "release": "scilab",
        "hermes_image": "registry.example/hermes@sha256:" + "a" * 64,
        "skills_image": "registry.example/skills@sha256:" + "b" * 64,
        "hermes_config_sha256": "d" * 64,
        "sandbox_image": "registry.example/sandbox@sha256:" + "e" * 64,
        "model_prices_thb": "{}",
    }
    kwargs.update(overrides)
    return build_run_worker(**kwargs)


def test_run_worker_gets_opa_url_from_secret() -> None:
    worker = _worker_for(opa_secret="scilab-opa-test")
    env = {item["name"]: item for item in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCILAB_OPA_URL"] == {
        "name": "SCILAB_OPA_URL",
        "valueFrom": {"secretKeyRef": {"name": "scilab-opa-test", "key": "SCILAB_OPA_URL"}},
    }


def test_run_worker_env_carries_hermes_and_sandbox_wiring() -> None:
    worker = _worker_for()
    env = {item["name"]: item.get("value") for item in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCILAB_HERMES_IMAGE"] == "registry.example/hermes@sha256:" + "a" * 64
    assert env["SCILAB_SKILLS_IMAGE"] == "registry.example/skills@sha256:" + "b" * 64
    assert env["SCILAB_HERMES_CONFIG_SHA256"] == "d" * 64
    assert env["SCILAB_SANDBOX_IMAGE"] == "registry.example/sandbox@sha256:" + "e" * 64
    assert env["SCILAB_MODEL_PRICES_THB"] == "{}"


def test_run_worker_rejects_unpinned_sandbox_image_and_bad_prices_json() -> None:
    with pytest.raises(ValueError, match="sandbox image"):
        _worker_for(sandbox_image="registry.example/sandbox:latest")
    with pytest.raises(ValueError, match="model prices"):
        _worker_for(model_prices_thb="not-json")


def test_hermes_and_run_worker_security_context_match_except_readonly_fs() -> None:
    hermes = by_kind(resources_for(), "Deployment")
    hermes_pod = hermes["spec"]["template"]["spec"]
    hermes_container = hermes_pod["containers"][0]
    hermes_init = hermes_pod["initContainers"][0]
    worker = _worker_for()
    worker_pod = worker["spec"]["template"]["spec"]
    worker_container = worker_pod["containers"][0]

    assert hermes_pod["automountServiceAccountToken"] is False
    assert worker_pod["automountServiceAccountToken"] is False
    assert hermes_pod["securityContext"] == {
        **worker_pod["securityContext"],
        "fsGroup": 10001,
    }
    assert hermes_container["securityContext"] == {
        **worker_container["securityContext"],
        "readOnlyRootFilesystem": False,
    }
    assert hermes_init["securityContext"] == hermes_container["securityContext"]


def test_deployment_uses_pinned_images_resources_secrets_and_read_only_skills() -> None:
    deployment = by_kind(resources_for(), "Deployment")
    pod = deployment["spec"]["template"]
    container = pod["spec"]["containers"][0]
    init = pod["spec"]["initContainers"][0]

    assert deployment["spec"]["replicas"] == 1
    assert deployment["spec"]["strategy"] == {"type": "Recreate"}
    assert container["image"] == lab_spec()["hermesImage"]
    assert container["command"] == ["hermes", "gateway"]
    assert init["image"] == lab_spec()["skillsImage"]
    assert init["command"] == ["/bin/sh", "-c", "cp -R /skills/. /skills-runtime/"]
    assert container["ports"] == [
        {"name": "api", "containerPort": 8642},
        {"name": "a2a", "containerPort": 9900},
    ]
    assert {item["name"]: item["value"] for item in container["env"] if "value" in item} == {
        "API_SERVER_ENABLED": "true",
        "HERMES_HOME": "/var/lib/hermes",
    }
    assert container["envFrom"] == [{"secretRef": {"name": "alpha-hermes-secrets"}}]
    assert container["resources"] == lab_spec()["resources"]
    assert {mount["mountPath"]: mount for mount in container["volumeMounts"]}[
        "/var/lib/hermes"
    ]["name"] == "hermes-home"
    skills_mount = {mount["mountPath"]: mount for mount in container["volumeMounts"]}["/var/lib/hermes/skills"]
    assert skills_mount["readOnly"] is True
    assert skills_mount["name"] == "skills-runtime"
    assert {volume["name"] for volume in pod["spec"]["volumes"]} == {
        "hermes-home",
        "skills-runtime",
        "hermes-config",
    }


def test_service_is_cluster_internal_and_exposes_only_required_ports() -> None:
    service = by_kind(resources_for(), "Service")

    assert service["spec"] == {
        "type": "ClusterIP",
        "selector": {
            "app.kubernetes.io/name": "hermes",
            "app.kubernetes.io/instance": "scilab-alpha",
        },
        "ports": [
            {"name": "api", "port": 8642, "targetPort": "api"},
            {"name": "a2a", "port": 9900, "targetPort": "a2a"},
        ],
    }
    assert "externalIPs" not in service["spec"]
    assert "loadBalancerIP" not in service["spec"]


def test_network_policy_allows_only_same_namespace_registered_sources() -> None:
    policy = by_kind(resources_for(), "NetworkPolicy")
    ingress = policy["spec"]["ingress"]
    from_selectors = [item["podSelector"]["matchLabels"] for item in ingress[0]["from"]]

    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "hermes",
        "app.kubernetes.io/instance": "scilab-alpha",
    }
    assert ingress[0]["ports"] == [
        {"protocol": "TCP", "port": 8642},
        {"protocol": "TCP", "port": 9900},
    ]
    assert from_selectors == [
        {"scilab.ai/component": "platform-gateway"},
        {"scilab.ai/component": "run-service", "scilab.ai/lab-id": "alpha"},
        {
            "scilab.ai/component": "hermes-peer",
            "scilab.ai/lab-id": "alpha",
            "scilab.ai/peer-registered": "true",
        },
    ]
    assert all("namespaceSelector" not in item and "ipBlock" not in item for item in ingress[0]["from"])


def _egress_policy(resources: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return next(
        resource
        for resource in resources
        if resource["kind"] == "NetworkPolicy" and resource["metadata"]["name"].endswith("-hermes-egress")
    )


def test_hermes_egress_policy_allows_only_dns_api_sandbox_and_external_cidrs() -> None:
    resources = resources_for(api_port=8000, opensandbox_port=8080, external_cidrs=("10.0.0.0/8",))
    policy = _egress_policy(resources)

    assert policy["metadata"]["name"] == "scilab-alpha-hermes-egress"
    assert policy["spec"]["policyTypes"] == ["Egress"]
    assert policy["spec"]["podSelector"]["matchLabels"] == {
        "app.kubernetes.io/name": "hermes",
        "app.kubernetes.io/instance": "scilab-alpha",
    }
    assert policy["spec"]["egress"] == [
        {"ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}]},
        {
            "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "scilab", "app.kubernetes.io/component": "api"}}}],
            "ports": [{"protocol": "TCP", "port": 8000}],
        },
        {
            "to": [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "opensandbox"}}}],
            "ports": [{"protocol": "TCP", "port": 8080}],
        },
        {"to": [{"ipBlock": {"cidr": "10.0.0.0/8"}}]},
    ]


def test_hermes_egress_policy_empty_cidrs_is_valid_no_external_egress() -> None:
    resources = resources_for(external_cidrs=())
    policy = _egress_policy(resources)

    assert len(policy["spec"]["egress"]) == 3
    assert all("ipBlock" not in rule.get("to", [{}])[0] for rule in policy["spec"]["egress"] if "to" in rule)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"api_port": None}, "SCILAB_API_PORT"),
        ({"api_port": 70000}, "SCILAB_API_PORT"),
        ({"opensandbox_port": "8080"}, "SCILAB_OPENSANDBOX_PORT"),
        ({"external_cidrs": ["not-a-cidr"]}, "SCILAB_EXTERNAL_CIDRS"),
        ({"external_cidrs": "10.0.0.0/8"}, "SCILAB_EXTERNAL_CIDRS"),
    ],
)
def test_hermes_egress_policy_is_fail_closed_on_bad_env(kwargs: dict[str, Any], message: str) -> None:
    from lab_operator.resources import build_resources

    base = {"api_port": 8000, "opensandbox_port": 8080, "external_cidrs": ()}
    base.update(kwargs)
    with pytest.raises(ValueError, match=message):
        build_resources(name="alpha", namespace="research", uid="uid-alpha", spec=lab_spec(), **base)


def test_reconcile_requires_api_port_opensandbox_port_and_cidrs(monkeypatch: pytest.MonkeyPatch) -> None:
    import lab_operator.handlers as handlers

    monkeypatch.delenv("SCILAB_API_PORT", raising=False)
    monkeypatch.delenv("SCILAB_OPENSANDBOX_PORT", raising=False)
    monkeypatch.delenv("SCILAB_EXTERNAL_CIDRS", raising=False)
    with pytest.raises(ValueError, match="SCILAB_API_PORT"):
        handlers.reconcile(spec=lab_spec(), name="alpha", namespace="research", uid="uid-alpha")


@pytest.mark.parametrize(
    ("name", "spec", "message"),
    [
        ("wrong-name", lab_spec(), "metadata.name must equal spec.labId"),
        ("alpha", {**lab_spec(), "hermesImage": "registry/hermes:latest"}, "digest"),
        ("alpha", {**lab_spec(), "secretRefs": [{"name": ""}]}, "secret"),
        ("alpha", {**lab_spec(), "resources": {"requests": {"cpu": "1"}}}, "resources"),
    ],
)
def test_invalid_spec_is_rejected_before_resource_build(
    name: str, spec: dict[str, Any], message: str
) -> None:
    from lab_operator.resources import build_resources

    with pytest.raises(ValueError, match=message):
        build_resources(
            name=name, namespace="research", uid="uid-alpha", spec=spec,
            api_port=8000, opensandbox_port=8080, external_cidrs=(),
        )


def test_apply_dispatches_each_kind_with_server_side_apply_arguments() -> None:
    from lab_operator.resources import apply_resources

    apps = FakeApi()
    core = FakeApi()
    networking = FakeApi()
    apply_resources(resources_for(), apps_api=apps, core_api=core, networking_api=networking)

    assert [call[0] for call in core.calls] == [
        "patch_namespaced_persistent_volume_claim",
        "patch_namespaced_config_map",
        "patch_namespaced_service",
    ]
    assert [call[0] for call in apps.calls] == ["patch_namespaced_deployment"]
    assert [call[0] for call in networking.calls] == [
        "patch_namespaced_network_policy",
        "patch_namespaced_network_policy",
    ]
    for api in (apps, core, networking):
        for _, args, kwargs in api.calls:
            assert kwargs["field_manager"] == "scilab-lab-operator"
            assert kwargs["force"] is True
            assert kwargs["_content_type"] == "application/apply-patch+yaml"
            assert args[1] == "research"
            assert args[2]["metadata"]["namespace"] == "research"


def test_reconcile_builds_and_applies_instead_of_returning_only_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    import lab_operator.handlers as handlers

    monkeypatch.setenv("SCILAB_API_PORT", "8000")
    monkeypatch.setenv("SCILAB_OPENSANDBOX_PORT", "8080")
    monkeypatch.setenv("SCILAB_EXTERNAL_CIDRS", "")
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(handlers, "apply_resources", lambda resources: calls.append(resources))
    result = handlers.reconcile(
        spec=lab_spec(), name="alpha", namespace="research", uid="uid-alpha"
    )

    assert result is None
    assert len(calls) == 1
    assert len(calls[0]) == 6
