import hashlib
from types import SimpleNamespace

import pytest

from scilab.artifacts import ArtifactService
from scilab.identity import Identity


def test_read_bytes_uses_authorized_artifact_and_checks_digest() -> None:
    identity = Identity("lab-a", "user:alice", frozenset({"artifacts:read"}))
    body = b"# Research report\n"
    artifact = SimpleNamespace(uri="s3://lab-a/run-1/report.md", sha256=hashlib.sha256(body).hexdigest())
    calls = []
    service = ArtifactService.__new__(ArtifactService)
    service.get = lambda actor, artifact_id: calls.append((actor, artifact_id)) or artifact
    service.storage = SimpleNamespace(read_bytes=lambda uri: body)

    assert service.read_bytes(identity, "report-1") == body
    assert calls == [(identity, "report-1")]

    service.storage = SimpleNamespace(read_bytes=lambda uri: b"tampered")
    with pytest.raises(ValueError, match="digest"):
        service.read_bytes(identity, "report-1")
