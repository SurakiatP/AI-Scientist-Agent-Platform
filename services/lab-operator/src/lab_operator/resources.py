from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from kubernetes import client, config


API_VERSION = "scilab.ai/v1alpha1"
FIELD_MANAGER = "scilab-lab-operator"
IMAGE_DIGEST = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")
SECRET_NAME = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


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
    *, name: str, namespace: str, uid: str, spec: Mapping[str, Any]
) -> tuple[dict[str, Any], ...]:
    lab_id = _validate_spec(name, namespace, uid, spec)
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
                    "initContainers": [
                        {
                            "name": "skills-init",
                            "image": spec["skillsImage"],
                            "command": ["/bin/sh", "-c", "cp -R /skills/. /skills-runtime/"],
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
                            ],
                            "envFrom": secret_env,
                            "resources": spec["resources"],
                            "volumeMounts": [
                                {"name": "hermes-home", "mountPath": "/var/lib/hermes"},
                                {
                                    "name": "skills-runtime",
                                    "mountPath": "/var/lib/hermes/skills",
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
                        {"podSelector": {"matchLabels": {"scilab.ai/component": "run-service"}}},
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
    return pvc, deployment, service, network_policy


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
        elif kind == "Service":
            core_api.patch_namespaced_service(name, namespace, body, **kwargs)
        elif kind == "NetworkPolicy":
            networking_api.patch_namespaced_network_policy(name, namespace, body, **kwargs)
        else:
            raise ValueError(f"unsupported resource kind: {kind}")
