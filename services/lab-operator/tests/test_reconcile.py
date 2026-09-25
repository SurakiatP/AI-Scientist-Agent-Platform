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


def resources_for(lab_id: str = "alpha") -> tuple[dict[str, Any], ...]:
    from lab_operator.resources import build_resources

    return build_resources(
        name=lab_id,
        namespace="research",
        uid=f"uid-{lab_id}",
        spec=lab_spec(lab_id),
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
    ]
    assert [resource["metadata"]["name"] for resource in first] == [
        "scilab-alpha-hermes-home",
        "scilab-alpha-hermes-config",
        "scilab-alpha-hermes",
        "scilab-alpha-hermes",
        "scilab-alpha-hermes-ingress",
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


def test_run_worker_gets_opa_url_from_secret() -> None:
    from lab_operator.resources import build_run_worker

    worker = build_run_worker(
        "alpha", "research", "uid-alpha", "registry.example/worker@sha256:" + "c" * 64,
        "scilab-postgres", "scilab-nats", "pi", "reviewer", "scilab-minio",
        lab_spec()["resources"], "scilab", opa_secret="scilab-opa-test",
    )
    env = {item["name"]: item for item in worker["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCILAB_OPA_URL"] == {
        "name": "SCILAB_OPA_URL",
        "valueFrom": {"secretKeyRef": {"name": "scilab-opa-test", "key": "SCILAB_OPA_URL"}},
    }


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
        build_resources(name=name, namespace="research", uid="uid-alpha", spec=spec)


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
    assert [call[0] for call in networking.calls] == ["patch_namespaced_network_policy"]
    for api in (apps, core, networking):
        for _, args, kwargs in api.calls:
            assert kwargs["field_manager"] == "scilab-lab-operator"
            assert kwargs["force"] is True
            assert kwargs["_content_type"] == "application/apply-patch+yaml"
            assert args[1] == "research"
            assert args[2]["metadata"]["namespace"] == "research"


def test_reconcile_builds_and_applies_instead_of_returning_only_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    import lab_operator.handlers as handlers

    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(handlers, "apply_resources", lambda resources: calls.append(resources))
    result = handlers.reconcile(
        spec=lab_spec(), name="alpha", namespace="research", uid="uid-alpha"
    )

    assert result is None
    assert len(calls) == 1
    assert len(calls[0]) == 5
