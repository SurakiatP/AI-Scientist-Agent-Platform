from __future__ import annotations

import os
from typing import Any, Mapping

import kopf

from lab_operator.resources import apply_resources, build_resources, build_run_worker


@kopf.on.create("scilab.ai", "v1alpha1", "labs")
@kopf.on.update("scilab.ai", "v1alpha1", "labs", field="spec")
@kopf.on.resume("scilab.ai", "v1alpha1", "labs")
def reconcile(
    spec: Mapping[str, Any], name: str, namespace: str, uid: str, **_: Any
) -> None:
    resources = build_resources(name=name, namespace=namespace, uid=uid, spec=spec)
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
    }
    if any(worker_config.values()):
        for key, value in worker_config.items():
            if not value:
                raise ValueError(f"{key} is required")
        resources += (build_run_worker(
            name, namespace, uid, worker_image, database_secret,
            worker_config["SCILAB_NATS_SECRET_NAME"],
            worker_config["SCILAB_PI_PROVIDER"],
            worker_config["SCILAB_REVIEWER_PROVIDER"],
            worker_config["SCILAB_MINIO_SECRET_NAME"],
            spec["resources"],
            worker_config["SCILAB_RELEASE_NAME"],
            opa_secret=os.getenv("SCILAB_OPA_SECRET_NAME", f'{worker_config["SCILAB_RELEASE_NAME"]}-opa'),
        ),)
    apply_resources(resources)
