from __future__ import annotations

import base64
import io
import json
from importlib import import_module

import pytest


def test_resolves_only_matching_lab_secret_and_internal_endpoint() -> None:
    seen = []

    def fetch(request, **kwargs):
        seen.append((request, kwargs))
        return io.BytesIO(json.dumps({
            "metadata": {"name": "scilab-lab-a-hermes-api", "namespace": "scilab"},
            "data": {"api_key": base64.b64encode(b"private-key").decode()},
        }).encode())

    resolver = import_module("scilab.api.hermes_credential").HermesSecretResolver(
        namespace="scilab", bearer_token="service-token", ssl_context=object(), fetch=fetch,
    )
    assert resolver.resolve("lab-a") == (
        "http://scilab-lab-a-hermes.scilab.svc.cluster.local:8642", "private-key",
    )
    assert seen[0][0].full_url.endswith("/namespaces/scilab/secrets/scilab-lab-a-hermes-api")
    assert seen[0][0].get_header("Authorization") == "Bearer service-token"


def test_rejects_cross_lab_secret_and_invalid_lab_id_without_leaking_key() -> None:
    calls = []

    def fetch(_request, **_kwargs):
        calls.append(True)
        return io.BytesIO(json.dumps({
            "metadata": {"name": "scilab-lab-b-hermes-api", "namespace": "scilab"},
            "data": {"api_key": base64.b64encode(b"private-key").decode()},
        }).encode())

    resolver = import_module("scilab.api.hermes_credential").HermesSecretResolver(
        namespace="scilab", bearer_token="service-token", ssl_context=object(), fetch=fetch,
    )
    with pytest.raises(ValueError):
        resolver.resolve("../lab-b")
    assert calls == []
    with pytest.raises(RuntimeError) as error:
        resolver.resolve("lab-a")
    assert "private-key" not in str(error.value)


def test_namespace_uses_kubernetes_label_limit_not_lab_id_limit() -> None:
    namespace = "n" * 41
    seen = []

    def fetch(request, **_kwargs):
        seen.append(request.full_url)
        return io.BytesIO(json.dumps({
            "metadata": {"name": "scilab-lab-a-hermes-api", "namespace": namespace},
            "data": {"api_key": base64.b64encode(b"key").decode()},
        }).encode())

    resolver = import_module("scilab.api.hermes_credential").HermesSecretResolver(
        namespace=namespace, bearer_token="service-token", ssl_context=object(), fetch=fetch,
    )
    assert resolver.resolve("lab-a")[1] == "key"
    assert f"/namespaces/{namespace}/secrets/" in seen[0]
