from __future__ import annotations

from typing import Any, Mapping

import kopf

from lab_operator.resources import apply_resources, build_resources


@kopf.on.create("scilab.ai", "v1alpha1", "labs")
@kopf.on.update("scilab.ai", "v1alpha1", "labs", field="spec")
@kopf.on.resume("scilab.ai", "v1alpha1", "labs")
def reconcile(
    spec: Mapping[str, Any], name: str, namespace: str, uid: str, **_: Any
) -> None:
    apply_resources(build_resources(name=name, namespace=namespace, uid=uid, spec=spec))
