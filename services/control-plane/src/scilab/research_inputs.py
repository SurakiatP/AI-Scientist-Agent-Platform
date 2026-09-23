from collections.abc import Sequence
from urllib.parse import urlsplit

from scilab.artifacts import ArtifactNotFound, ArtifactService
from scilab.identity import Identity


def validate_research_inputs(
    identity: Identity,
    inputs: Sequence[str] | None,
    artifacts: ArtifactService,
) -> list[str]:
    if inputs is None:
        return []
    if isinstance(inputs, (str, bytes)) or not isinstance(inputs, Sequence):
        raise ValueError("inputs must be a sequence of artifact IDs or HTTP(S) URLs")

    validated: list[str] = []
    for value in inputs:
        if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
            raise ValueError("each input must be a non-blank artifact ID or HTTP(S) URL")

        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ValueError("research URLs must be absolute HTTP(S) URLs") from exc

        if parsed.scheme:
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.netloc
                or not hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("research URLs must be absolute HTTP(S) URLs without credentials")
            validated.append(value)
            continue

        artifact = artifacts.get(identity, value)
        if artifact.lab_id != identity.lab_id:
            raise ArtifactNotFound("artifact not found")
        validated.append(artifact.id)

    return validated
