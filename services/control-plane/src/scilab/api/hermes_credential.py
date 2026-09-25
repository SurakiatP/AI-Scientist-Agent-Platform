"""Resolve a Lab's Hermes API key from its Kubernetes Secret."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable
from urllib.request import Request, urlopen


_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")


def _label(value: str, max_length: int) -> str:
    if not isinstance(value, str) or len(value) > max_length or not _DNS_LABEL.fullmatch(value):
        raise ValueError("invalid Lab or namespace label")
    return value


class HermesSecretResolver:
    def __init__(
        self,
        *,
        namespace: str,
        bearer_token: str,
        ssl_context: Any,
        fetch: Callable[..., Any] = urlopen,
    ) -> None:
        self.namespace = _label(namespace, 63)
        if not isinstance(bearer_token, str) or not bearer_token.strip() or "\n" in bearer_token:
            raise ValueError("service-account token is required")
        self.bearer_token = bearer_token
        self.ssl_context = ssl_context
        self.fetch = fetch

    def resolve(self, lab_id: str) -> tuple[str, str]:
        lab_id = _label(lab_id, 40)
        name = f"scilab-{lab_id}-hermes-api"
        request = Request(
            f"https://kubernetes.default.svc/api/v1/namespaces/{self.namespace}/secrets/{name}",
            headers={"Authorization": f"Bearer {self.bearer_token}", "Accept": "application/json"},
        )
        try:
            with self.fetch(request, context=self.ssl_context, timeout=5) as response:
                secret = json.load(response)
            if secret["metadata"]["name"] != name or secret["metadata"]["namespace"] != self.namespace:
                raise ValueError("wrong Secret identity")
            api_key = base64.b64decode(secret["data"]["api_key"], validate=True).decode("utf-8")
            if not api_key.strip() or any(char in api_key for char in "\r\n\x00"):
                raise ValueError("invalid API key")
        except Exception:
            raise RuntimeError("Lab Hermes credential unavailable") from None
        return f"http://scilab-{lab_id}-hermes.{self.namespace}.svc.cluster.local:8642", api_key
