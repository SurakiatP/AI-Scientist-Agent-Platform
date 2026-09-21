import asyncio
from datetime import timedelta

import pytest

from scilab.runs.model import RunState
from scilab.sandbox import (
    OpenSandboxSDKClient,
    SandboxBroker,
    SandboxHandle,
    SandboxRunCoordinator,
    validate_fqdn_allowlist,
)


class FakeSandbox:
    id = "sandbox-1"


class RecordingSandboxClient:
    def __init__(self) -> None:
        self.sandbox = FakeSandbox()
        self.calls: list[tuple[str, object]] = []

    async def create(self, *, image, timeout, metadata, allowed_hosts):
        self.calls.append(
            (
                "create",
                {
                    "image": image,
                    "timeout": timeout,
                    "metadata": metadata,
                    "allowed_hosts": allowed_hosts,
                },
            )
        )
        return self.sandbox

    async def run(self, sandbox, command):
        self.calls.append(("run", {"sandbox": sandbox, "command": command}))
        return {"exit_code": 0, "stdout": "ok"}

    async def upload(self, sandbox, path, content):
        self.calls.append(
            ("upload", {"sandbox": sandbox, "path": path, "content": content})
        )

    async def download(self, sandbox, path):
        self.calls.append(("download", {"sandbox": sandbox, "path": path}))
        return b"artifact"

    async def destroy(self, sandbox):
        self.calls.append(("destroy", {"sandbox": sandbox}))


class RecordingVaultBinder:
    def __init__(self) -> None:
        self.calls: list[tuple[object, str]] = []

    async def bind(self, sandbox, vault_ref):
        self.calls.append((sandbox, vault_ref))


class RaisingVaultBinder:
    def __init__(self, error_type) -> None:
        self.error_type = error_type

    async def bind(self, sandbox, vault_ref):
        raise self.error_type("bind failed")


def test_non_code_role_does_not_allocate_sandbox():
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client)

        result = await broker.for_role("writer", needs_tools=False)

        assert result is None
        assert client.calls == []

    asyncio.run(scenario())


def test_code_agent_gets_ephemeral_kata_sandbox_with_run_expiry():
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client)
        timeout = timedelta(minutes=7)

        handle = await broker.create_for_agent(
            "agent-1",
            "run-1",
            lab_id="lab-a",
            needs_tools=True,
            run_timeout=timeout,
            allowed_hosts=("api.anthropic.com",),
        )

        assert handle.id == "sandbox-1"
        request = client.calls[0][1]
        assert request["timeout"] == timeout
        assert request["allowed_hosts"] == ("api.anthropic.com",)
        assert request["metadata"] == {
            "scilab.ai/agent-id": "agent-1",
            "scilab.ai/lab-id": "lab-a",
            "scilab.ai/run-id": "run-1",
            "scilab.ai/workload": "sandbox",
        }

    asyncio.run(scenario())


def test_vault_binder_receives_opaque_sandbox_and_same_lab_reference_only():
    async def scenario():
        client = RecordingSandboxClient()
        binder = RecordingVaultBinder()
        broker = SandboxBroker(client, vault_binder=binder)

        await broker.create_for_agent(
            "agent-1",
            "run-1",
            lab_id="lab-a",
            needs_tools=True,
            run_timeout=timedelta(minutes=5),
            credential_vault_ref="vault://lab-a/anthropic",
        )

        assert binder.calls == [(client.sandbox, "vault://lab-a/anthropic")]
        request = client.calls[0][1]
        assert "credential_vault_ref" not in request["metadata"]

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", (RuntimeError, asyncio.CancelledError))
def test_vault_bind_failure_always_destroys_created_sandbox(error_type):
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client, vault_binder=RaisingVaultBinder(error_type))

        with pytest.raises(error_type):
            await broker.create_for_agent(
                "agent-1",
                "run-1",
                lab_id="lab-a",
                needs_tools=True,
                run_timeout=timedelta(minutes=5),
                credential_vault_ref="vault://lab-a/anthropic",
            )

        assert [call[0] for call in client.calls] == ["create", "destroy"]

    asyncio.run(scenario())


def test_sandbox_runs_exports_artifact_and_cleans_up():
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client)
        handle = await broker.create_for_agent(
            "agent-1",
            "run-1",
            lab_id="lab-a",
            needs_tools=True,
            run_timeout=timedelta(minutes=5),
        )

        result = await broker.run(handle, ["python", "-c", "print('ok')"])
        await broker.upload(handle, "/workspace/result.txt", b"result")
        artifact = await broker.download(handle, "/workspace/result.txt")
        await broker.destroy(handle)

        assert result == {"exit_code": 0, "stdout": "ok"}
        assert artifact == b"artifact"
        assert [call[0] for call in client.calls[-4:]] == [
            "run",
            "upload",
            "download",
            "destroy",
        ]

    asyncio.run(scenario())


def test_non_terminal_run_does_not_destroy_and_terminal_run_does():
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client)
        handle = await broker.create_for_agent(
            "agent-1",
            "run-1",
            lab_id="lab-a",
            needs_tools=True,
            run_timeout=timedelta(minutes=5),
        )

        assert await broker.cleanup_for_run_state(handle, RunState.RUNNING) is False
        assert [call[0] for call in client.calls] == ["create"]
        assert await broker.cleanup_for_run_state(handle, RunState.COMPLETED) is True
        assert [call[0] for call in client.calls] == ["create", "destroy"]

    asyncio.run(scenario())


def test_vault_reference_cannot_cross_lab_or_contain_plaintext_secret():
    async def scenario():
        broker = SandboxBroker(RecordingSandboxClient())

        with pytest.raises(ValueError, match="Vault reference"):
            await broker.create_for_agent(
                "agent-1",
                "run-1",
                lab_id="lab-a",
                needs_tools=True,
                run_timeout=timedelta(minutes=5),
                credential_vault_ref="sk-live-plaintext",
            )

        with pytest.raises(ValueError, match="same lab"):
            await broker.create_for_agent(
                "agent-1",
                "run-1",
                lab_id="lab-a",
                needs_tools=True,
                run_timeout=timedelta(minutes=5),
                credential_vault_ref="vault://lab-b/anthropic",
            )

    asyncio.run(scenario())


def test_fqdn_allowlist_accepts_exact_and_leftmost_wildcard():
    assert validate_fqdn_allowlist(
        ("API.Example.com", "*.packages.example.org")
    ) == ("api.example.com", "*.packages.example.org")


@pytest.mark.parametrize(
    "value",
    (
        "",
        "api.example.com:443",
        "https://api.example.com",
        "127.0.0.1",
        "*.example.com/path",
        "*.*.example.com",
    ),
)
def test_fqdn_allowlist_rejects_blank_url_ip_port_and_non_leftmost_wildcard(value):
    with pytest.raises(ValueError):
        validate_fqdn_allowlist((value,))


class FakeNetworkRule:
    def __init__(self, *, action, target):
        self.action = action
        self.target = target


class FakeNetworkPolicy:
    def __init__(self, *, default_action, egress):
        self.default_action = default_action
        self.egress = egress


class FakeCredentialProxyConfig:
    def __init__(self, *, enabled):
        self.enabled = enabled


class FakeSDKFactory:
    arguments = None

    @classmethod
    async def create(
        cls,
        image,
        *,
        timeout,
        metadata,
        network_policy,
        credential_proxy,
    ):
        cls.arguments = {
            "image": image,
            "timeout": timeout,
            "metadata": metadata,
            "network_policy": network_policy,
            "credential_proxy": credential_proxy,
        }
        return FakeSandbox()


def test_opensandbox_sdk_adapter_uses_release_1_1_0_sdk_signature():
    async def scenario():
        adapter = OpenSandboxSDKClient(
            sandbox_cls=FakeSDKFactory,
            network_policy_cls=FakeNetworkPolicy,
            network_rule_cls=FakeNetworkRule,
            credential_proxy_cls=FakeCredentialProxyConfig,
        )

        sandbox = await adapter.create(
            image="ubuntu:22.04",
            timeout=timedelta(minutes=5),
            metadata={"scilab.ai/workload": "sandbox"},
            allowed_hosts=("api.example.com", "*.packages.example.org"),
        )

        assert sandbox.id == "sandbox-1"
        args = FakeSDKFactory.arguments
        assert args["image"] == "ubuntu:22.04"
        assert args["timeout"] == timedelta(minutes=5)
        assert args["metadata"] == {"scilab.ai/workload": "sandbox"}
        assert args["network_policy"].default_action == "deny"
        assert [(rule.action, rule.target) for rule in args["network_policy"].egress] == [
            ("allow", "api.example.com"),
            ("allow", "*.packages.example.org"),
        ]
        assert args["credential_proxy"].enabled is True

    asyncio.run(scenario())


class FakeFiles:
    def __init__(self, content):
        self.content = content

    async def read_bytes(self, path):
        return self.content


def test_sdk_download_uses_binary_api():
    async def scenario():
        sandbox = FakeSandbox()
        sandbox.files = FakeFiles(b"\x00artifact\xff")

        assert await OpenSandboxSDKClient().download(sandbox, "/artifact") == b"\x00artifact\xff"

    asyncio.run(scenario())


class FakeTransitionService:
    def __init__(self) -> None:
        self.calls = []

    def transition(self, identity, run_id, target, **kwargs):
        self.calls.append((identity, run_id, target, kwargs))
        return type("TransitionedRun", (), {"id": run_id, "state": target})()


@pytest.mark.parametrize(
    "terminal_state",
    (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED),
)
def test_run_coordinator_cleans_all_handles_only_after_terminal_transition(terminal_state):
    async def scenario():
        client = RecordingSandboxClient()
        broker = SandboxBroker(client)
        service = FakeTransitionService()
        coordinator = SandboxRunCoordinator(service, broker)
        sandboxes = [type("TrackedSandbox", (), {"id": f"sandbox-{n}"})() for n in (1, 2)]
        for n, sandbox in enumerate(sandboxes, start=1):
            coordinator.register(
                SandboxHandle(
                    id=sandbox.id,
                    agent_id=f"agent-{n}",
                    run_id="run-1",
                    lab_id="lab-a",
                    timeout=timedelta(minutes=5),
                    sandbox=sandbox,
                )
            )

        identity = object()
        running = await coordinator.transition(identity, "run-1", RunState.RUNNING)
        assert running.state is RunState.RUNNING
        assert not [call for call in client.calls if call[0] == "destroy"]

        terminal = await coordinator.transition(
            identity, "run-1", terminal_state, reason="terminal"
        )
        assert terminal.state is terminal_state
        assert [call[1]["sandbox"].id for call in client.calls if call[0] == "destroy"] == [
            "sandbox-1",
            "sandbox-2",
        ]
        assert service.calls[-1] == (
            identity,
            "run-1",
            terminal_state,
            {"reason": "terminal"},
        )

    asyncio.run(scenario())
