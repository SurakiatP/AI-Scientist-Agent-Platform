from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).parents[2]
CHART = ROOT / "deploy/helm/scilab"
TEST_DIGESTS = {
    "images.api.repository": "registry.test/scilab-api",
    "images.api.digest": "a" * 64,
    "images.web.repository": "registry.test/scilab-web",
    "images.web.digest": "f" * 64,
    "images.runWorker.repository": "registry.test/scilab-run-worker",
    "images.runWorker.digest": "b" * 64,
    "images.labOperator.repository": "registry.test/scilab-lab-operator",
    "images.labOperator.digest": "c" * 64,
    "images.openSandbox.execd.digest": "d" * 64,
    "images.openSandbox.egress.digest": "e" * 64,
}
TEST_OVERRIDES = {
    **TEST_DIGESTS,
    "externalSecrets.postgres": "scilab-postgres-test",
    "externalSecrets.minio": "scilab-minio-test",
    "externalSecrets.nats": "scilab-nats-test",
    "externalSecrets.keycloak": "scilab-keycloak-test",
    "externalSecrets.opa": "scilab-opa-test",
    "externalSecrets.telemetry": "scilab-telemetry-test",
    "runWorker.piProvider": "test-pi-provider",
    "runWorker.reviewerProvider": "test-reviewer-provider",
    "runAdmissionPerMinute": "7",
    "network.externalCidrs[0]": "10.33.0.0/16",
    "api.ingress.enabled": "true",
    "api.ingress.host": "gateway.test.invalid",
    "api.ingress.tlsSecretName": "scilab-gateway-tls-test",
}


def test_web_and_app_network_policies_are_rendered() -> None:
    documents = _render("staging", TEST_OVERRIDES)
    by_name = {document["metadata"]["name"]: document for document in documents if "metadata" in document}
    web = by_name["scilab-web"]
    assert web["kind"] == "Deployment"
    container = web["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "registry.test/scilab-web@sha256:" + "f" * 64
    assert {item["name"]: item["value"] for item in container["env"]}["SCILAB_API_ORIGIN"] == "http://scilab-api:8000"
    assert by_name["scilab-web-service"]["spec"]["type"] == "ClusterIP"
    deny = by_name["scilab-app-default-deny"]
    assert deny["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in deny["spec"] and "egress" not in deny["spec"]
    egress = by_name["scilab-worker-egress"]["spec"]["egress"]
    assert {"to": [{"ipBlock": {"cidr": "10.33.0.0/16"}}]} in egress
    assert all("to" in rule for rule in egress)
    assert all("to" in rule for rule in by_name["scilab-app-egress"]["spec"]["egress"])
    assert by_name["scilab-worker-default-deny"]["spec"]["podSelector"]["matchLabels"] == {
        "scilab.ai/component": "run-service", "scilab.ai/platform-release": "scilab"
    }


def test_chart_rejects_missing_external_egress_cidrs() -> None:
    overrides = {key: value for key, value in TEST_OVERRIDES.items() if key != "network.externalCidrs[0]"}
    result = _helm("template", "scilab", str(CHART), *_override_args(overrides))
    assert result.returncode != 0
    assert "network.externalCidrs" in result.stderr


def test_optional_web_tls_ingress_routes_to_web_only() -> None:
    overrides = {**TEST_OVERRIDES, "web.ingress.enabled": "true", "web.ingress.host": "research.test.invalid", "web.ingress.tlsSecretName": "web-tls-test"}
    documents = _render("staging", overrides)
    web_ingress = next(doc for doc in documents if doc["kind"] == "Ingress" and doc["metadata"]["name"] == "scilab-web")
    backend = web_ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "scilab-web-service"
    assert backend["port"]["number"] == 3000
    assert web_ingress["spec"]["tls"][0]["secretName"] == "web-tls-test"


def test_empty_trusted_proxy_cidrs_renders_trust_none_and_prices_render_as_json() -> None:
    documents = _render("staging", TEST_OVERRIDES)
    deployments = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Deployment"
    }
    api = deployments["scilab-api"]["spec"]["template"]["spec"]["containers"][0]
    assert api["command"][-1] == "--forwarded-allow-ips="
    operator_env = {
        item["name"]: item
        for item in deployments["scilab-lab-operator"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
    }
    assert operator_env["SCILAB_MODEL_PRICES_THB"]["value"] == "{}"


def _helm(*args: str) -> subprocess.CompletedProcess[str]:
    assert shutil.which("helm"), "helm is required for the chart acceptance gate"
    return subprocess.run(
        ["helm", *args], cwd=ROOT, text=True, capture_output=True, check=False
    )


def _override_args(overrides: dict[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in overrides.items():
        flag = "--set" if key == "api.ingress.enabled" else "--set-string"
        args.extend((flag, f"{key}={value}"))
    return args


def _render(profile: str, overrides: dict[str, str]) -> list[dict]:
    result = _helm(
        "template",
        "--include-crds",
        "scilab",
        str(CHART),
        "-f",
        str(CHART / f"values-{profile}.yaml"),
        *_override_args(overrides),
    )
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


@pytest.mark.parametrize("profile", ["staging", "production"])
def test_chart_lints_and_renders_only_secure_application_boundaries(profile: str) -> None:
    lint = _helm(
        "lint",
        "--strict",
        str(CHART),
        "-f",
        str(CHART / f"values-{profile}.yaml"),
        *_override_args(TEST_OVERRIDES),
    )
    assert lint.returncode == 0, lint.stdout + lint.stderr

    documents = _render(profile, TEST_OVERRIDES)
    deployments = {
        container["name"]: deployment
        for deployment in documents
        if deployment["kind"] == "Deployment"
        for container in deployment["spec"]["template"]["spec"]["containers"]
    }
    assert set(deployments) == {"api", "web", "lab-operator"}

    for deployment in deployments.values():
        pod_spec = deployment["spec"]["template"]["spec"]
        all_containers = pod_spec["containers"] + pod_spec.get("initContainers", [])
        assert all(
            re.fullmatch(r".+@sha256:[0-9a-f]{64}", container["image"])
            for container in all_containers
        )
    assert not any(document["kind"] == "Secret" for document in documents)
    assert any(document["kind"] == "NetworkPolicy" for document in documents)

    services = [document for document in documents if document["kind"] == "Service"]
    assert {service["metadata"]["name"] for service in services} == {
        "scilab-api", "scilab-web-service"
    }
    assert all(service["spec"]["type"] == "ClusterIP" for service in services)
    api_service = next(service for service in services if service["metadata"]["name"] == "scilab-api")
    api_pod_labels = deployments["api"]["spec"]["template"]["metadata"]["labels"]
    assert api_pod_labels["scilab.ai/component"] == "platform-gateway"
    api_pod = deployments["api"]["spec"]["template"]["spec"]
    assert api_pod["securityContext"]["runAsNonRoot"] is True
    assert api_pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"] is True
    assert "scilab.ai/component" not in api_service["spec"]["selector"]
    assert {port["port"] for port in api_service["spec"]["ports"]}.isdisjoint(
        {8642, 9900}
    )

    ingresses = [document for document in documents if document["kind"] == "Ingress"]
    assert len(ingresses) == 1
    backend = ingresses[0]["spec"]["rules"][0]["http"]["paths"][0]["backend"]
    assert backend["service"]["name"] == "scilab-api"
    assert ingresses[0]["spec"]["tls"] == [
        {
            "hosts": ["gateway.test.invalid"],
            "secretName": "scilab-gateway-tls-test",
        }
    ]

    crds = [
        document
        for document in documents
        if document["kind"] == "CustomResourceDefinition"
    ]
    assert [crd["metadata"]["name"] for crd in crds] == ["labs.scilab.ai"]

    api = deployments["api"]["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item for item in api["env"]}
    expected_secret_env = {
        "SCILAB_DATABASE_URL": ("scilab-postgres-test", "SCILAB_DATABASE_URL"),
        "SCILAB_OIDC_ISSUER": ("scilab-keycloak-test", "SCILAB_OIDC_ISSUER"),
        "SCILAB_OIDC_AUDIENCE": ("scilab-keycloak-test", "SCILAB_OIDC_AUDIENCE"),
        "SCILAB_OIDC_JWKS_URI": ("scilab-keycloak-test", "SCILAB_OIDC_JWKS_URI"),
        "SCILAB_KEYCLOAK_REALM_URL": ("scilab-keycloak-test", "SCILAB_KEYCLOAK_REALM_URL"),
        "SCILAB_MCP_BASE_URL": ("scilab-keycloak-test", "SCILAB_MCP_BASE_URL"),
        "SCILAB_MINIO_URL": ("scilab-minio-test", "SCILAB_MINIO_URL"),
        "SCILAB_MINIO_ACCESS_KEY": ("scilab-minio-test", "SCILAB_MINIO_ACCESS_KEY"),
        "SCILAB_MINIO_SECRET_KEY": ("scilab-minio-test", "SCILAB_MINIO_SECRET_KEY"),
        "SCILAB_INPUT_BUCKET": ("scilab-minio-test", "SCILAB_INPUT_BUCKET"),
        "SCILAB_ARTIFACT_BUCKET": ("scilab-minio-test", "SCILAB_ARTIFACT_BUCKET"),
        "SCILAB_NATS_URL": ("scilab-nats-test", "SCILAB_NATS_URL"),
        "SCILAB_OPA_URL": ("scilab-opa-test", "SCILAB_OPA_URL"),
    }
    assert set(env) == set(expected_secret_env) | {
        "POD_NAMESPACE",
        "SCILAB_RUN_ADMISSION_PER_MINUTE",
    }
    for name, (secret_name, key) in expected_secret_env.items():
        assert env[name]["valueFrom"]["secretKeyRef"] == {
            "name": secret_name,
            "key": key,
        }
    assert env["SCILAB_RUN_ADMISSION_PER_MINUTE"]["value"] == "7"
    assert api["command"] == [
        "uvicorn",
        "scilab.api.runtime:create_runtime_app",
        "--factory",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--proxy-headers",
        "--forwarded-allow-ips=",
    ]
    api_secret_refs = {
        item["secretRef"]["name"] for item in api["envFrom"]
    }
    assert api_secret_refs == {"scilab-telemetry-test"}
    operator = deployments["lab-operator"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    operator_env = {item["name"]: item for item in operator.get("env", [])}
    assert set(operator_env) == {
        "SCILAB_RUN_WORKER_IMAGE",
        "SCILAB_DATABASE_SECRET_NAME",
        "SCILAB_NATS_SECRET_NAME",
        "SCILAB_MINIO_SECRET_NAME",
        "SCILAB_OPA_SECRET_NAME",
        "SCILAB_RELEASE_NAME",
        "SCILAB_PI_PROVIDER",
        "SCILAB_REVIEWER_PROVIDER",
        "SCILAB_API_PORT",
        "SCILAB_OPENSANDBOX_PORT",
        "SCILAB_EXTERNAL_CIDRS",
        "SCILAB_SANDBOX_IMAGE",
        "SCILAB_MODEL_PRICES_THB",
    }
    assert operator_env["SCILAB_API_PORT"]["value"] == "8000"
    assert operator_env["SCILAB_OPENSANDBOX_PORT"]["value"] == "8080"
    assert operator_env["SCILAB_EXTERNAL_CIDRS"]["value"] == "10.33.0.0/16"
    assert operator_env["SCILAB_SANDBOX_IMAGE"]["value"] == (
        "opensandbox/execd:release-1.1.0@sha256:" + "d" * 64
    )
    assert operator_env["SCILAB_MODEL_PRICES_THB"]["value"] == "{}"
    assert operator_env["SCILAB_DATABASE_SECRET_NAME"]["value"] == (
        "scilab-postgres-test"
    )
    assert operator_env["SCILAB_NATS_SECRET_NAME"]["value"] == "scilab-nats-test"
    assert operator_env["SCILAB_MINIO_SECRET_NAME"]["value"] == "scilab-minio-test"
    assert operator_env["SCILAB_RELEASE_NAME"]["value"] == "scilab"
    assert operator_env["SCILAB_PI_PROVIDER"]["value"] == "test-pi-provider"
    assert operator_env["SCILAB_REVIEWER_PROVIDER"]["value"] == (
        "test-reviewer-provider"
    )
    assert operator_env["SCILAB_RUN_WORKER_IMAGE"]["value"] == (
        "registry.test/scilab-run-worker@sha256:" + "b" * 64
    )
    for deployment in deployments.values():
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            assert all(
                "secretRef" in item for item in container.get("envFrom", [])
            )
            assert all(
            "value" not in item
            or item["name"]
            in {
                "SCILAB_RUN_ADMISSION_PER_MINUTE",
                "SCILAB_RUN_WORKER_IMAGE",
                "SCILAB_DATABASE_SECRET_NAME",
                "SCILAB_NATS_SECRET_NAME",
                "SCILAB_MINIO_SECRET_NAME",
                "SCILAB_OPA_SECRET_NAME",
                "SCILAB_RELEASE_NAME",
                "SCILAB_PI_PROVIDER",
                "SCILAB_REVIEWER_PROVIDER",
                "SCILAB_API_ORIGIN",
                "SCILAB_API_PORT",
                "SCILAB_OPENSANDBOX_PORT",
                "SCILAB_EXTERNAL_CIDRS",
                "SCILAB_SANDBOX_IMAGE",
                "SCILAB_MODEL_PRICES_THB",
            }
            for item in container.get("env", [])
        )

    config_maps = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "ConfigMap"
    }
    sandbox = config_maps["scilab-opensandbox-config"]["data"]
    sandbox_toml = sandbox["sandbox.toml"]
    assert 'type = "kata"' in sandbox_toml
    assert 'k8s_runtime_class = "kata-qemu"' in sandbox_toml
    assert 'mode = "dns+nft"' in sandbox_toml
    assert f"@sha256:{'d' * 64}" in sandbox_toml
    assert f"@sha256:{'e' * 64}" in sandbox_toml
    defaults = yaml.safe_load(sandbox["sandbox-request-defaults.yaml"])
    assert defaults["credentialProxy"] == {"enabled": True}
    assert defaults["networkPolicy"]["defaultAction"] == "deny"
    assert defaults["networkPolicy"]["allowedHosts"] == []
    assert defaults["networkPolicy"]["dns"]["mode"] == "dns+nft"
    observability = config_maps["scilab-observability-config"]["data"][
        "observability.yaml"
    ]
    assert "baggage: [run_id, lab_id]" in observability
    assert "requireVirtualKey: true" in observability


@pytest.mark.parametrize(
    ("invalid", "expected_error"),
    [
        ({"runAdmissionPerMinute": ""}, "SCILAB_RUN_ADMISSION_PER_MINUTE"),
        ({"externalSecrets.postgres": ""}, "externalSecrets.postgres"),
        ({"externalSecrets.minio": ""}, "externalSecrets.minio"),
        ({"externalSecrets.nats": ""}, "externalSecrets.nats"),
        ({"externalSecrets.keycloak": ""}, "externalSecrets.keycloak"),
        ({"externalSecrets.opa": ""}, "externalSecrets.opa"),
        ({"externalSecrets.telemetry": ""}, "externalSecrets.telemetry"),
        ({"runWorker.piProvider": ""}, "runWorker.piProvider"),
        ({"runWorker.reviewerProvider": ""}, "runWorker.reviewerProvider"),
        (
            {
                "runWorker.piProvider": "same-provider",
                "runWorker.reviewerProvider": "same-provider",
            },
            "must differ",
        ),
        ({"images.api.digest": ""}, "images.api.digest"),
        ({"images.runWorker.digest": ""}, "images.runWorker.digest"),
        ({"api.ingress.tlsSecretName": ""}, "api.ingress.tlsSecretName"),
    ],
)
def test_chart_rejects_missing_required_value_or_digest(
    invalid: dict[str, str], expected_error: str
) -> None:
    overrides = {**TEST_OVERRIDES, **invalid}
    result = _helm(
        "template",
        "scilab",
        str(CHART),
        "-f",
        str(CHART / "values-staging.yaml"),
        *_override_args(overrides),
    )
    assert result.returncode != 0
    assert expected_error in result.stderr


@pytest.mark.parametrize("invalid_rate", ["0", "-1", "1.5", "unlimited"])
def test_chart_rejects_nonpositive_or_noninteger_admission_rate(
    invalid_rate: str,
) -> None:
    overrides = {**TEST_OVERRIDES, "runAdmissionPerMinute": invalid_rate}
    result = _helm(
        "template",
        "scilab",
        str(CHART),
        "-f",
        str(CHART / "values-staging.yaml"),
        *_override_args(overrides),
    )
    assert result.returncode != 0
    assert "SCILAB_RUN_ADMISSION_PER_MINUTE must be a positive integer" in result.stderr


def test_api_secret_access_is_namespace_scoped_and_get_only() -> None:
    documents = _render("staging", TEST_OVERRIDES)
    roles = [
        document
        for document in documents
        if document["kind"] == "Role" and document["metadata"]["name"] == "scilab-api"
    ]
    assert len(roles) == 1
    assert roles[0]["rules"] == [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}
    ]

    bindings = [
        document
        for document in documents
        if document["kind"] == "RoleBinding"
        and document["metadata"]["name"] == "scilab-api"
    ]
    assert len(bindings) == 1
    assert bindings[0]["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "scilab-api",
    }
    assert bindings[0]["subjects"] == [
        {"kind": "ServiceAccount", "name": "scilab-api", "namespace": "default"}
    ]

    api = next(
        document
        for document in documents
        if document["kind"] == "Deployment"
        and document["metadata"]["name"] == "scilab-api"
    )
    pod_spec = api["spec"]["template"]["spec"]
    assert pod_spec["serviceAccountName"] == "scilab-api"
    assert pod_spec["automountServiceAccountToken"] is True
    namespace = next(
        item
        for item in pod_spec["containers"][0]["env"]
        if item["name"] == "POD_NAMESPACE"
    )
    assert namespace["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"


def test_lab_operator_child_resource_permissions_are_patch_only() -> None:
    documents = _render("staging", TEST_OVERRIDES)
    role = next(
        document
        for document in documents
        if document["kind"] == "ClusterRole"
        and document["metadata"]["name"] == "scilab-lab-operator"
    )
    child_resources = {
        "persistentvolumeclaims",
        "configmaps",
        "services",
        "deployments",
        "networkpolicies",
    }
    child_rules = [
        rule
        for rule in role["rules"]
        if child_resources.intersection(rule["resources"])
    ]
    assert {
        resource
        for rule in child_rules
        for resource in rule["resources"]
    } == child_resources
    assert all(rule["verbs"] == ["patch"] for rule in child_rules)
    assert all("secrets" not in rule["resources"] for rule in role["rules"])


def test_lab_operator_uses_opa_secret_reference_and_can_apply_configmaps() -> None:
    documents = _render("staging", TEST_OVERRIDES)
    role = next(doc for doc in documents if doc["kind"] == "ClusterRole" and doc["metadata"]["name"] == "scilab-lab-operator")
    assert any(rule["apiGroups"] == [""] and "configmaps" in rule["resources"] and rule["verbs"] == ["patch"] for rule in role["rules"])
    operator = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "scilab-lab-operator")
    env = {item["name"]: item for item in operator["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["SCILAB_OPA_SECRET_NAME"] == {"name": "SCILAB_OPA_SECRET_NAME", "value": "scilab-opa-test"}
    assert "SCILAB_OPA_URL" not in env
