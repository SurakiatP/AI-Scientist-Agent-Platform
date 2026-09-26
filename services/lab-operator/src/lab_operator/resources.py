from __future__ import annotations

import ipaddress
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from kubernetes import client, config


API_VERSION = "scilab.ai/v1alpha1"
FIELD_MANAGER = "scilab-lab-operator"
IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SECRET_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")

# Control-plane API pod labels, mirrored from deploy/helm/scilab/templates/api.yaml.
API_POD_SELECTOR = {"app.kubernetes.io/name": "scilab", "app.kubernetes.io/component": "api"}
# OpenSandbox broker pod label, mirrored from deploy/helm/scilab/templates/opensandbox.yaml.
OPENSANDBOX_POD_SELECTOR = {"app.kubernetes.io/name": "opensandbox"}


def _pod_security_context(*, fs_group: int | None = None) -> dict[str, Any]:
    context: dict[str, Any] = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    if fs_group is not None:
        context["fsGroup"] = fs_group
    return context


def _container_security_context(*, read_only_root_filesystem: bool) -> dict[str, Any]:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": read_only_root_filesystem,
        "capabilities": {"drop": ["ALL"]},
    }


def _validate_port(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if not (1 <= value <= 65535):
        raise ValueError(f"{field} must be a valid TCP port")
    return value


def _validate_cidrs(values: Any, field: str) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{field} must be a list of CIDR strings")
    cidrs: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} entries must be non-empty strings")
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError as exc:
            raise ValueError(f"{field} contains an invalid CIDR: {item}") from exc
        cidrs.append(item)
    return cidrs


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_spec(name: str, namespace: str, uid: str, spec: Mapping[str, Any]) -> str:
    _require_string(namespace, "namespace")
    _require_string(uid, "uid")
    required = {"labId", "hermesImage", "skillsImage", "pvcSize", "resources", "secretRefs"}
    if set(spec) != required:
        missing = required - set(spec)
        unknown = set(spec) - required
        details = ", ".join(sorted(missing or unknown))
        raise ValueError(f"resources spec is invalid: {details}")

    lab_id = _require_string(spec["labId"], "labId")
    if name != lab_id:
        raise ValueError("metadata.name must equal spec.labId")
    if len(lab_id) > 40 or not DNS_LABEL.fullmatch(lab_id):
        raise ValueError("labId must be a DNS label of at most 40 characters")
    for field in ("hermesImage", "skillsImage"):
        image = _require_string(spec[field], field)
        if not IMAGE_DIGEST.fullmatch(image):
            raise ValueError(f"{field} must use a sha256 digest")

    _require_string(spec["pvcSize"], "pvcSize")
    resources = spec["resources"]
    if not isinstance(resources, Mapping) or set(resources) != {"requests", "limits"}:
        raise ValueError("resources must contain requests and limits")
    for tier in ("requests", "limits"):
        values = resources[tier]
        if not isinstance(values, Mapping) or set(values) != {"cpu", "memory"}:
            raise ValueError("resources must contain cpu and memory")
        for field in ("cpu", "memory"):
            _require_string(values[field], f"resources.{tier}.{field}")

    secret_refs = spec["secretRefs"]
    if not isinstance(secret_refs, list):
        raise ValueError("secretRefs must be a list")
    for secret_ref in secret_refs:
        if not isinstance(secret_ref, Mapping) or set(secret_ref) != {"name"}:
            raise ValueError("secret reference must contain only name")
        secret_name = _require_string(secret_ref["name"], "secret reference name")
        if not SECRET_NAME.fullmatch(secret_name):
            raise ValueError("secret reference name is invalid")
    return lab_id


def _metadata(name: str, namespace: str, uid: str, labels: dict[str, str]) -> dict[str, Any]:
    return {
        "name": name,
        "namespace": namespace,
        "labels": labels,
        "ownerReferences": [
            {
                "apiVersion": API_VERSION,
                "kind": "Lab",
                "name": labels["scilab.ai/lab-id"],
                "uid": uid,
                "controller": True,
                "blockOwnerDeletion": True,
            }
        ],
    }


def build_resources(
    *,
    name: str,
    namespace: str,
    uid: str,
    spec: Mapping[str, Any],
    api_port: int,
    opensandbox_port: int,
    external_cidrs: Sequence[str],
) -> tuple[dict[str, Any], ...]:
    lab_id = _validate_spec(name, namespace, uid, spec)
    api_port = _validate_port(api_port, "SCILAB_API_PORT")
    opensandbox_port = _validate_port(opensandbox_port, "SCILAB_OPENSANDBOX_PORT")
    external_cidrs = _validate_cidrs(external_cidrs, "SCILAB_EXTERNAL_CIDRS")
    labels = {
        "app.kubernetes.io/name": "hermes",
        "app.kubernetes.io/instance": f"scilab-{lab_id}",
        "app.kubernetes.io/managed-by": FIELD_MANAGER,
        "scilab.ai/component": "hermes",
        "scilab.ai/lab-id": lab_id,
    }
    selector = {
        "app.kubernetes.io/name": "hermes",
        "app.kubernetes.io/instance": f"scilab-{lab_id}",
    }
    secret_env = [{"secretRef": {"name": item["name"]}} for item in spec["secretRefs"]]
    owner_metadata = lambda resource_name: _metadata(resource_name, namespace, uid, labels)

    pvc = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": owner_metadata(f"scilab-{lab_id}-hermes-home"),
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": spec["pvcSize"]}},
        },
    }
    hermes_config_name = f"scilab-{lab_id}-hermes-config"
    hermes_config = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": owner_metadata(hermes_config_name),
        "data": {"config.yaml": "approvals:\n  mode: manual\n  timeout: 86400\n"},
    }
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": owner_metadata(f"scilab-{lab_id}-hermes"),
        "spec": {
            "replicas": 1,
            "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": _pod_security_context(fs_group=10001),
                    "initContainers": [
                        {
                            "name": "skills-init",
                            "image": spec["skillsImage"],
                            "command": ["/bin/sh", "-c", "cp -R /skills/. /skills-runtime/"],
                            "securityContext": _container_security_context(read_only_root_filesystem=False),
                            "volumeMounts": [
                                {"name": "skills-runtime", "mountPath": "/skills-runtime"}
                            ],
                        }
                    ],
                    "containers": [
                        {
                "name": "hermes",
                "image": spec["hermesImage"],
                "command": ["hermes", "gateway"],
                "ports": [
                                {"name": "api", "containerPort": 8642},
                                {"name": "a2a", "containerPort": 9900},
                            ],
                        "env": [
                            {"name": "API_SERVER_ENABLED", "value": "true"},
                            {"name": "HERMES_HOME", "value": "/var/lib/hermes"},
                            {"name": "API_SERVER_KEY", "valueFrom": {"secretKeyRef": {"name": f"scilab-{lab_id}-hermes-api", "key": "api_key"}}},
                        ],
                            "envFrom": secret_env,
                            "resources": spec["resources"],
                            "securityContext": _container_security_context(read_only_root_filesystem=False),
                            "volumeMounts": [
                                {"name": "hermes-home", "mountPath": "/var/lib/hermes"},
                                {
                                    "name": "skills-runtime",
                                    "mountPath": "/var/lib/hermes/skills",
                                    "readOnly": True,
                                },
                                {
                                    "name": "hermes-config",
                                    "mountPath": "/var/lib/hermes/config.yaml",
                                    "subPath": "config.yaml",
                                    "readOnly": True,
                                },
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "hermes-home",
                            "persistentVolumeClaim": {"claimName": f"scilab-{lab_id}-hermes-home"},
                        },
                        {"name": "skills-runtime", "emptyDir": {}},
                        {"name": "hermes-config", "configMap": {"name": hermes_config_name}},
                    ],
                },
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": owner_metadata(f"scilab-{lab_id}-hermes"),
        "spec": {
            "type": "ClusterIP",
            "selector": selector,
            "ports": [
                {"name": "api", "port": 8642, "targetPort": "api"},
                {"name": "a2a", "port": 9900, "targetPort": "a2a"},
            ],
        },
    }
    network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": owner_metadata(f"scilab-{lab_id}-hermes-ingress"),
        "spec": {
            "podSelector": {"matchLabels": selector},
            "policyTypes": ["Ingress"],
            "ingress": [
                {
                    "from": [
                        {"podSelector": {"matchLabels": {"scilab.ai/component": "platform-gateway"}}},
                    {"podSelector": {"matchLabels": {"scilab.ai/component": "run-service", "scilab.ai/lab-id": lab_id}}},
                        {
                            "podSelector": {
                                "matchLabels": {
                                    "scilab.ai/component": "hermes-peer",
                                    "scilab.ai/lab-id": lab_id,
                                    "scilab.ai/peer-registered": "true",
                                }
                            }
                        },
                    ],
                    "ports": [
                        {"protocol": "TCP", "port": 8642},
                        {"protocol": "TCP", "port": 9900},
                    ],
                }
            ],
        },
    }
    egress_rules: list[dict[str, Any]] = [
        {
            "ports": [
                {"protocol": "UDP", "port": 53},
                {"protocol": "TCP", "port": 53},
            ]
        },
        {
            "to": [{"podSelector": {"matchLabels": API_POD_SELECTOR}}],
            "ports": [{"protocol": "TCP", "port": api_port}],
        },
        {
            "to": [{"podSelector": {"matchLabels": OPENSANDBOX_POD_SELECTOR}}],
            "ports": [{"protocol": "TCP", "port": opensandbox_port}],
        },
    ]
    # ponytail: DNS rule is port-only (no `to`) since CoreDNS pod labels vary
    # by cluster/CNI; tighten with a podSelector once the cluster's DNS
    # labels are known.
    for cidr in external_cidrs:
        egress_rules.append({"to": [{"ipBlock": {"cidr": cidr}}]})
    egress_network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": owner_metadata(f"scilab-{lab_id}-hermes-egress"),
        "spec": {
            "podSelector": {"matchLabels": selector},
            "policyTypes": ["Egress"],
            "egress": egress_rules,
        },
    }
    return pvc, hermes_config, deployment, service, network_policy, egress_network_policy


def build_run_worker(
    lab_id: str,
    namespace: str,
    uid: str,
    image: str,
    database_secret: str,
    nats_secret: str,
    pi_provider: str,
    reviewer_provider: str,
    minio_secret: str,
    resources: Mapping[str, Any],
    release: str,
    *,
    hermes_image: str,
    skills_image: str,
    hermes_config_sha256: str,
    sandbox_image: str,
    model_prices_thb: str,
    opa_secret: str = "scilab-opa",
) -> dict[str, Any]:
    if len(lab_id) > 40 or not DNS_LABEL.fullmatch(lab_id):
        raise ValueError("labId must be a DNS label of at most 40 characters")
    _require_string(namespace, "namespace")
    _require_string(uid, "uid")
    if not IMAGE_DIGEST.fullmatch(image):
        raise ValueError("run worker image must be pinned by digest")
    if not SECRET_NAME.fullmatch(database_secret):
        raise ValueError("database secret name is invalid")
    if not SECRET_NAME.fullmatch(nats_secret):
        raise ValueError("NATS secret name is invalid")
    if not SECRET_NAME.fullmatch(minio_secret):
        raise ValueError("MinIO secret name is invalid")
    if not SECRET_NAME.fullmatch(opa_secret):
        raise ValueError("OPA secret name is invalid")
    if not DNS_LABEL.fullmatch(release):
        raise ValueError("release name must be a DNS label")
    pi_provider = _require_string(pi_provider, "PI provider")
    reviewer_provider = _require_string(reviewer_provider, "Reviewer provider")
    if pi_provider == reviewer_provider:
        raise ValueError("Reviewer provider must differ from PI provider")
    if not IMAGE_DIGEST.fullmatch(hermes_image):
        raise ValueError("Hermes image must be pinned by digest")
    if not IMAGE_DIGEST.fullmatch(skills_image):
        raise ValueError("skills image must be pinned by digest")
    if not IMAGE_DIGEST.fullmatch(sandbox_image):
        raise ValueError("sandbox image must be pinned by digest")
    hermes_config_sha256 = _require_string(hermes_config_sha256, "Hermes config sha256")
    model_prices_thb = _require_string(model_prices_thb, "model prices")
    try:
        json.loads(model_prices_thb)
    except ValueError as exc:
        raise ValueError("model prices must be valid JSON") from exc

    labels = {
        "app.kubernetes.io/name": "scilab-run-worker",
        "app.kubernetes.io/instance": f"scilab-{lab_id}",
        "app.kubernetes.io/managed-by": FIELD_MANAGER,
        "scilab.ai/component": "run-service",
        "scilab.ai/lab-id": lab_id,
        "scilab.ai/platform-release": release,
    }
    name = f"scilab-{lab_id}-run-worker"
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": _metadata(name, namespace, uid, labels),
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "automountServiceAccountToken": False,
                    "securityContext": _pod_security_context(),
                    "containers": [{
                        "name": "run-worker",
                        "image": image,
                        "command": ["python", "-m", "scilab.runs.worker_main"],
                        "securityContext": _container_security_context(read_only_root_filesystem=True),
                        "resources": resources,
                        "envFrom": [{"secretRef": {"name": minio_secret}}],
                        "env": [
                            {"name": "SCILAB_LAB_ID", "value": lab_id},
                            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
                            {"name": "SCILAB_DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": database_secret, "key": "SCILAB_DATABASE_URL"}}},
                            {"name": "SCILAB_NATS_URL", "valueFrom": {"secretKeyRef": {"name": nats_secret, "key": "SCILAB_NATS_URL"}}},
                            {"name": "SCILAB_PI_PROVIDER", "value": pi_provider},
                            {"name": "SCILAB_REVIEWER_PROVIDER", "value": reviewer_provider},
                            {"name": "SCILAB_HERMES_API_KEY", "valueFrom": {"secretKeyRef": {"name": f"scilab-{lab_id}-hermes-api", "key": "api_key"}}},
                            {"name": "SCILAB_OPA_URL", "valueFrom": {"secretKeyRef": {"name": opa_secret, "key": "SCILAB_OPA_URL"}}},
                            {"name": "SCILAB_HERMES_IMAGE", "value": hermes_image},
                            {"name": "SCILAB_SKILLS_IMAGE", "value": skills_image},
                            {"name": "SCILAB_HERMES_CONFIG_SHA256", "value": hermes_config_sha256},
                            {"name": "SCILAB_SANDBOX_IMAGE", "value": sandbox_image},
                            {"name": "SCILAB_MODEL_PRICES_THB", "value": model_prices_thb},
                        ],
                    }],
                },
            },
        },
    }


def apply_resources(
    resources: Iterable[Mapping[str, Any]],
    *,
    apps_api: Any | None = None,
    core_api: Any | None = None,
    networking_api: Any | None = None,
) -> None:
    if apps_api is None or core_api is None or networking_api is None:
        config.load_incluster_config()
        apps_api = apps_api or client.AppsV1Api()
        core_api = core_api or client.CoreV1Api()
        networking_api = networking_api or client.NetworkingV1Api()
    for resource in resources:
        metadata = resource["metadata"]
        name = metadata["name"]
        namespace = metadata["namespace"]
        body = dict(resource)
        kind = resource["kind"]
        kwargs = {
            "field_manager": FIELD_MANAGER,
            "force": True,
            "_content_type": "application/apply-patch+yaml",
        }
        if kind == "Deployment":
            apps_api.patch_namespaced_deployment(name, namespace, body, **kwargs)
        elif kind == "PersistentVolumeClaim":
            core_api.patch_namespaced_persistent_volume_claim(name, namespace, body, **kwargs)
        elif kind == "ConfigMap":
            core_api.patch_namespaced_config_map(name, namespace, body, **kwargs)
        elif kind == "Service":
            core_api.patch_namespaced_service(name, namespace, body, **kwargs)
        elif kind == "NetworkPolicy":
            networking_api.patch_namespaced_network_policy(name, namespace, body, **kwargs)
        else:
            raise ValueError(f"unsupported resource kind: {kind}")
