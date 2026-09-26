from __future__ import annotations

import hashlib
import os
from typing import Any, Mapping

import kopf

from lab_operator.resources import apply_resources, build_resources, build_run_worker


def _require_int_env(name: str) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _parse_cidrs_env(name: str) -> list[str]:
    value = os.getenv(name)
    if value is None:
        raise ValueError(f"{name} is required")
    return [item.strip() for item in value.split(",") if item.strip()]


@kopf.on.create("scilab.ai", "v1alpha1", "labs")
@kopf.on.update("scilab.ai", "v1alpha1", "labs", field="spec")
@kopf.on.resume("scilab.ai", "v1alpha1", "labs")
def reconcile(
    spec: Mapping[str, Any], name: str, namespace: str, uid: str, **_: Any
) -> None:
    resources = build_resources(
        name=name,
        namespace=namespace,
        uid=uid,
        spec=spec,
        api_port=_require_int_env("SCILAB_API_PORT"),
        opensandbox_port=_require_int_env("SCILAB_OPENSANDBOX_PORT"),
        external_cidrs=_parse_cidrs_env("SCILAB_EXTERNAL_CIDRS"),
    )
    worker_image = os.getenv("SCILAB_RUN_WORKER_IMAGE")
    database_secret = os.getenv("SCILAB_DATABASE_SECRET_NAME")
    worker_config = {
        "SCILAB_RUN_WORKER_IMAGE": worker_image,
        "SCILAB_DATABASE_SECRET_NAME": database_secret,
        "SCILAB_NATS_SECRET_NAME": os.getenv("SCILAB_NATS_SECRET_NAME"),
        "SCILAB_MINIO_SECRET_NAME": os.getenv("SCILAB_MINIO_SECRET_NAME"),
        "SCILAB_RELEASE_NAME": os.getenv("SCILAB_RELEASE_NAME"),
        "SCILAB_PI_PROVIDER": os.getenv("SCILAB_PI_PROVIDER"),
        "SCILAB_REVIEWER_PROVIDER": os.getenv("SCILAB_REVIEWER_PROVIDER"),
        "SCILAB_SANDBOX_IMAGE": os.getenv("SCILAB_SANDBOX_IMAGE"),
        "SCILAB_MODEL_PRICES_THB": os.getenv("SCILAB_MODEL_PRICES_THB"),
    }
    if any(worker_config.values()):
        for key, value in worker_config.items():
            if not value:
                raise ValueError(f"{key} is required")
        hermes_config = next(r for r in resources if r["kind"] == "ConfigMap")
        config_sha256 = hashlib.sha256(
            hermes_config["data"]["config.yaml"].encode("utf-8")
        ).hexdigest()
        resources += (build_run_worker(
            name, namespace, uid, worker_image, database_secret,
            worker_config["SCILAB_NATS_SECRET_NAME"],
            worker_config["SCILAB_PI_PROVIDER"],
            worker_config["SCILAB_REVIEWER_PROVIDER"],
            worker_config["SCILAB_MINIO_SECRET_NAME"],
            spec["resources"],
            worker_config["SCILAB_RELEASE_NAME"],
            hermes_image=spec["hermesImage"],
            skills_image=spec["skillsImage"],
            hermes_config_sha256=config_sha256,
            sandbox_image=worker_config["SCILAB_SANDBOX_IMAGE"],
            model_prices_thb=worker_config["SCILAB_MODEL_PRICES_THB"],
            opa_secret=os.getenv("SCILAB_OPA_SECRET_NAME", f'{worker_config["SCILAB_RELEASE_NAME"]}-opa'),
        ),)
    apply_resources(resources)
