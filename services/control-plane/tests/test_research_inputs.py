from dataclasses import dataclass

import pytest

from scilab.artifacts import ArtifactNotFound
from scilab.identity import Identity
from scilab.research_inputs import validate_research_inputs


@dataclass(frozen=True)
class StubArtifact:
    id: str
    lab_id: str


class ArtifactLookup:
    def __init__(self, artifacts: list[StubArtifact]) -> None:
        self.artifacts = {artifact.id: artifact for artifact in artifacts}
        self.calls: list[tuple[Identity, str]] = []

    def get(self, identity: Identity, artifact_id: str) -> StubArtifact:
        self.calls.append((identity, artifact_id))
        artifact = self.artifacts.get(artifact_id)
        if artifact is None or artifact.lab_id != identity.lab_id:
            raise ArtifactNotFound("artifact not found")
        return artifact


def test_artifact_input_resolves_through_authenticated_lab_lookup() -> None:
    identity = Identity("lab-a", "user:1", frozenset({"artifacts:read"}))
    artifacts = ArtifactLookup([StubArtifact("artifact-1", "lab-a")])

    assert validate_research_inputs(identity, ["artifact-1"], artifacts) == ["artifact-1"]
    assert artifacts.calls == [(identity, "artifact-1")]


def test_artifact_from_another_lab_is_not_found() -> None:
    identity = Identity("lab-a", "user:1", frozenset({"artifacts:read"}))
    artifacts = ArtifactLookup([StubArtifact("artifact-2", "lab-b")])

    with pytest.raises(ArtifactNotFound, match="artifact not found"):
        validate_research_inputs(identity, ["artifact-2"], artifacts)


def test_http_url_is_preserved_without_artifact_lookup() -> None:
    identity = Identity("lab-a", "user:1", frozenset({"artifacts:read"}))
    artifacts = ArtifactLookup([])
    url = "https://data.example.org/paper.csv?version=2"

    assert validate_research_inputs(identity, [url], artifacts) == [url]
    assert artifacts.calls == []
