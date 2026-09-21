import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from ipaddress import ip_address
import re
from typing import Any, Protocol

from opensandbox.models.filesystem import WriteEntry
from opensandbox.models.sandboxes import (
    CredentialProxyConfig,
    NetworkPolicy,
    NetworkRule,
)
from opensandbox.sandbox import Sandbox

from scilab.runs.model import RunState


OPEN_SANDBOX_RELEASE = "release-1.1.0"
OPEN_SANDBOX_PACKAGE_VERSION = "1.1.0"
KATA_RUNTIME_CLASS = "kata-qemu"
_FQDN_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_FQDN = re.compile(rf"(?:{_FQDN_LABEL}\.)+{_FQDN_LABEL}\Z")
_TERMINAL_STATES = {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED}


def validate_fqdn_allowlist(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise ValueError("FQDN allowlist must be an iterable of host names")

    validated: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("FQDN must be text")
        host = value.strip().lower()
        if not host:
            raise ValueError("FQDN cannot be blank")
        wildcard = host.startswith("*.")
        candidate = host[2:] if wildcard else host
        if "*" in candidate or ("*" in host and not wildcard):
            raise ValueError("only a leftmost FQDN wildcard is allowed")
        if len(candidate) > 253 or not _FQDN.fullmatch(candidate):
            raise ValueError("FQDN must be an exact host name or leftmost wildcard")
        try:
            ip_address(candidate)
        except ValueError:
            pass
        else:
            raise ValueError("IP addresses are not FQDNs")
        normalized = f"*.{candidate}" if wildcard else candidate
        if normalized not in validated:
            validated.append(normalized)
    return tuple(validated)


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    id: str
    agent_id: str
    run_id: str
    lab_id: str
    timeout: timedelta
    sandbox: Any = field(repr=False, compare=False)


class CredentialVaultBinder(Protocol):
    async def bind(self, sandbox: Any, vault_ref: str) -> None:
        """Bind an opaque Vault reference without exposing its secret value."""


class SandboxClient(Protocol):
    async def create(
        self,
        *,
        image: str,
        timeout: timedelta,
        metadata: Mapping[str, str],
        allowed_hosts: tuple[str, ...],
    ) -> Any: ...

    async def run(self, sandbox: Any, command: Any) -> Any: ...

    async def upload(self, sandbox: Any, path: str, content: bytes) -> Any: ...

    async def download(self, sandbox: Any, path: str) -> bytes: ...

    async def destroy(self, sandbox: Any) -> Any: ...


class OpenSandboxSDKClient:
    """Thin adapter for the pinned OpenSandbox 1.1.0 async SDK."""

    def __init__(
        self,
        *,
        sandbox_cls: Any = Sandbox,
        network_policy_cls: Any = NetworkPolicy,
        network_rule_cls: Any = NetworkRule,
        credential_proxy_cls: Any = CredentialProxyConfig,
        write_entry_cls: Any = WriteEntry,
    ) -> None:
        self.sandbox_cls = sandbox_cls
        self.network_policy_cls = network_policy_cls
        self.network_rule_cls = network_rule_cls
        self.credential_proxy_cls = credential_proxy_cls
        self.write_entry_cls = write_entry_cls

    async def create(
        self,
        *,
        image: str,
        timeout: timedelta,
        metadata: Mapping[str, str],
        allowed_hosts: tuple[str, ...],
    ) -> Any:
        rules = [
            self.network_rule_cls(action="allow", target=host)
            for host in allowed_hosts
        ]
        network_policy = self.network_policy_cls(
            default_action="deny",
            egress=rules,
        )
        credential_proxy = self.credential_proxy_cls(enabled=True)
        return await self.sandbox_cls.create(
            image,
            timeout=timeout,
            metadata=dict(metadata),
            network_policy=network_policy,
            credential_proxy=credential_proxy,
        )

    async def run(self, sandbox: Any, command: Any) -> Any:
        return await sandbox.commands.run(command)

    async def upload(self, sandbox: Any, path: str, content: bytes) -> Any:
        entry = self.write_entry_cls(path=path, data=content)
        return await sandbox.files.write_files([entry])

    async def download(self, sandbox: Any, path: str) -> bytes:
        return await sandbox.files.read_bytes(path)

    async def destroy(self, sandbox: Any) -> Any:
        return await sandbox.destroy()


class SandboxBroker:
    def __init__(
        self,
        client: SandboxClient,
        *,
        image: str = "ubuntu:22.04",
        vault_binder: CredentialVaultBinder | None = None,
    ) -> None:
        self.client = client
        self.image = image
        self.vault_binder = vault_binder

    async def for_role(
        self,
        role: str,
        *,
        needs_tools: bool,
        run_id: str | None = None,
        lab_id: str | None = None,
        run_timeout: timedelta = timedelta(minutes=120),
        credential_vault_ref: str | None = None,
        allowed_hosts: Iterable[str] = (),
    ) -> SandboxHandle | None:
        if not needs_tools:
            return None
        if run_id is None or lab_id is None:
            raise ValueError("run_id and lab_id are required for a tool-capable role")
        return await self.create_for_agent(
            role,
            run_id,
            lab_id=lab_id,
            needs_tools=True,
            run_timeout=run_timeout,
            credential_vault_ref=credential_vault_ref,
            allowed_hosts=allowed_hosts,
        )

    async def create_for_agent(
        self,
        agent_id: str,
        run_id: str,
        *,
        lab_id: str,
        needs_tools: bool,
        run_timeout: timedelta,
        credential_vault_ref: str | None = None,
        allowed_hosts: Iterable[str] = (),
    ) -> SandboxHandle | None:
        if not needs_tools:
            return None
        if not agent_id.strip() or not run_id.strip() or not lab_id.strip():
            raise ValueError("agent_id, run_id, and lab_id must be non-blank")
        if run_timeout <= timedelta(0):
            raise ValueError("run_timeout must be positive")
        hosts = validate_fqdn_allowlist(allowed_hosts)
        if credential_vault_ref is not None:
            if not credential_vault_ref.startswith("vault://"):
                raise ValueError("credential_vault_ref must be a Vault reference")
            if not credential_vault_ref.startswith(f"vault://{lab_id}/"):
                raise ValueError("credential_vault_ref must target the same lab")
            if self.vault_binder is None:
                raise ValueError("credential Vault binder is required")

        metadata = {
            "scilab.ai/agent-id": agent_id,
            "scilab.ai/lab-id": lab_id,
            "scilab.ai/run-id": run_id,
            "scilab.ai/workload": "sandbox",
        }
        sandbox = await self.client.create(
            image=self.image,
            timeout=run_timeout,
            metadata=metadata,
            allowed_hosts=hosts,
        )
        try:
            if credential_vault_ref is not None:
                await self.vault_binder.bind(sandbox, credential_vault_ref)
        except BaseException:
            cleanup = asyncio.create_task(self.client.destroy(sandbox))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
            raise
        return SandboxHandle(
            id=sandbox.id,
            agent_id=agent_id,
            run_id=run_id,
            lab_id=lab_id,
            timeout=run_timeout,
            sandbox=sandbox,
        )

    async def run(self, handle: SandboxHandle, command: Any) -> Any:
        return await self.client.run(handle.sandbox, command)

    async def upload(self, handle: SandboxHandle, path: str, content: bytes) -> Any:
        if not path.strip():
            raise ValueError("path must be non-blank")
        return await self.client.upload(handle.sandbox, path, content)

    async def download(self, handle: SandboxHandle, path: str) -> bytes:
        if not path.strip():
            raise ValueError("path must be non-blank")
        return await self.client.download(handle.sandbox, path)

    async def destroy(self, handle: SandboxHandle) -> Any:
        return await self.client.destroy(handle.sandbox)

    async def cleanup_for_run_state(
        self, handle: SandboxHandle, state: RunState | str
    ) -> bool:
        if RunState(state) not in _TERMINAL_STATES:
            return False
        await self.destroy(handle)
        return True


class SandboxRunCoordinator:
    """Transition Runs and release every sandbox owned by terminal Runs."""

    def __init__(self, run_service: Any, broker: SandboxBroker) -> None:
        self.run_service = run_service
        self.broker = broker
        self._handles: dict[str, list[SandboxHandle]] = {}

    def register(self, handle: SandboxHandle) -> None:
        self._handles.setdefault(handle.run_id, []).append(handle)

    async def transition(
        self,
        identity: Any,
        run_id: str,
        target: RunState | str,
        **kwargs: Any,
    ) -> Any:
        run = self.run_service.transition(identity, run_id, target, **kwargs)
        if RunState(run.state) not in _TERMINAL_STATES:
            return run

        handles = self._handles.pop(run_id, [])
        results = await asyncio.gather(
            *(self.broker.destroy(handle) for handle in handles),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return run
