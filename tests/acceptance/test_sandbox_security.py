import asyncio
import json
import os
import shlex
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

from scilab.runs.model import RunState
from scilab.sandbox import OpenSandboxSDKClient, SandboxBroker


ROOT = Path(__file__).parents[2]
OPEN_SANDBOX_MANIFEST = ROOT / "deploy/helm/scilab/templates/opensandbox.yaml"
EGRESS_POLICY = ROOT / "deploy/policies/sandbox-egress.yaml"


def _documents(path: Path) -> list[dict]:
    return [document for document in yaml.safe_load_all(path.read_text()) if document]


def test_opensandbox_manifest_is_structurally_secure():
    documents = _documents(OPEN_SANDBOX_MANIFEST)
    config = next(document for document in documents if document["kind"] == "ConfigMap")
    runtime = next(document for document in documents if document["kind"] == "RuntimeClass")
    defaults = yaml.safe_load(config["data"]["sandbox-request-defaults.yaml"])
    sandbox_toml = config["data"]["sandbox.toml"]

    assert config["data"]["release"] == "release-1.1.0"
    assert config["data"]["packageVersion"] == "1.1.0"
    assert runtime["metadata"]["name"] == "kata-qemu"
    assert runtime["handler"] == "kata-qemu"
    assert "release-1.1.0" in sandbox_toml
    assert defaults["credentialProxy"] == {"enabled": True}
    assert defaults["networkPolicy"]["defaultAction"] == "deny"
    assert defaults["networkPolicy"]["allowedHosts"] == []
    assert defaults["networkPolicy"]["dns"]["mode"] == "dns+nft"
    assert defaults["metadata"]["annotations"]["sidecar.istio.io/inject"] == "false"

    serialized = json.dumps(documents)
    assert "secretValue" not in serialized
    assert "plaintext" not in serialized.lower()


def test_egress_file_is_opensandbox_policy_input_not_kubernetes_network_policy():
    documents = _documents(EGRESS_POLICY)

    assert len(documents) == 1
    assert all(document.get("kind") != "NetworkPolicy" for document in documents)
    assert documents[0] == {
        "defaultAction": "deny",
        "allowedHosts": [],
        "dns": {"mode": "dns+nft"},
    }


def _live_required() -> None:
    required = (
        "SCILAB_SANDBOX_ALLOWED_URL",
        "SCILAB_SANDBOX_DENIED_URL",
        "SCILAB_SANDBOX_IMAGE",
        "OPEN_SANDBOX_DOMAIN",
        "OPEN_SANDBOX_API_KEY",
    )
    if os.getenv("SCILAB_SANDBOX_LIVE") != "1":
        pytest.skip("SCILAB_SANDBOX_LIVE=1 is required")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        pytest.skip(f"live sandbox infrastructure is not configured: {', '.join(missing)}")


def _request_command(url: str) -> str:
    script = f"import urllib.request; urllib.request.urlopen({url!r}, timeout=8).read(1)"
    return f"python -c {shlex.quote(script)}"


class ObservedSDKClient(OpenSandboxSDKClient):
    def __init__(self) -> None:
        super().__init__()
        self.destroyed_ids: list[str] = []

    async def destroy(self, sandbox):
        result = await super().destroy(sandbox)
        self.destroyed_ids.append(sandbox.id)
        return result


def test_live_sandbox_allows_and_denies_egress_then_destroys():
    _live_required()
    allowed_url = os.environ["SCILAB_SANDBOX_ALLOWED_URL"]
    denied_url = os.environ["SCILAB_SANDBOX_DENIED_URL"]
    allowed_host = urlsplit(allowed_url).hostname
    if allowed_host is None or urlsplit(denied_url).hostname is None:
        pytest.fail("live URLs must include an HTTP(S) host")

    async def scenario():
        adapter = ObservedSDKClient()
        broker = SandboxBroker(adapter, image=os.environ["SCILAB_SANDBOX_IMAGE"])
        handle = None
        try:
            handle = await asyncio.wait_for(
                broker.create_for_agent(
                    "acceptance-agent",
                    "acceptance-run",
                    lab_id="acceptance-lab",
                    needs_tools=True,
                    run_timeout=timedelta(seconds=90),
                    allowed_hosts=(allowed_host,),
                ),
                timeout=90,
            )
            policy = await asyncio.wait_for(handle.sandbox.get_egress_policy(), timeout=15)
            assert policy.default_action == "deny"

            allowed = await asyncio.wait_for(
                broker.run(handle, _request_command(allowed_url)), timeout=30
            )
            assert allowed.exit_code == 0

            try:
                denied = await asyncio.wait_for(
                    broker.run(handle, _request_command(denied_url)), timeout=30
                )
            except Exception:
                denied_blocked = True
            else:
                denied_blocked = denied.exit_code not in (None, 0)
            assert denied_blocked
        finally:
            if handle is not None:
                assert await asyncio.wait_for(
                    broker.cleanup_for_run_state(handle, RunState.COMPLETED), timeout=30
                )
        assert adapter.destroyed_ids == [handle.id]

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))


def test_live_credential_probe_needs_vault_fixture():
    _live_required()
    pytest.xfail("safe credential injection probe needs a non-secret Vault fixture")
