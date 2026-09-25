from pathlib import Path
from typing import Any

import pytest
import yaml
from jsonschema import Draft202012Validator
from openapi_spec_validator import validate


OPENAPI_PATH = Path(__file__).parents[2] / "contracts/openapi/scilab.yaml"
INPUTS_PATH = "/v1/labs/{lab}/inputs"
API_KEYS_PATH = "/v1/labs/{lab}/api-keys"
PEERS_PATH = "/v1/labs/{lab}/peers"


@pytest.fixture(scope="module")
def document() -> dict[str, Any]:
    return yaml.safe_load(OPENAPI_PATH.read_text())


def _resolve(document: dict[str, Any], value: Any) -> Any:
    if isinstance(value, list):
        return [_resolve(document, item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value:
        target = document
        for part in value["$ref"].removeprefix("#/").split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return _resolve(document, target)
    return {key: _resolve(document, item) for key, item in value.items()}


def _operation_schema(
    document: dict[str, Any], path: str, method: str, *, response: bool = False
) -> dict[str, Any]:
    operation = document["paths"][path][method]
    if response:
        message = _resolve(document, operation["responses"]["200"])
    else:
        message = _resolve(document, operation["requestBody"])
    schema = message["content"]["application/json"]["schema"]
    return _resolve(document, schema)


def test_openapi_document_is_valid(document: dict[str, Any]) -> None:
    validate(document)


def test_run_options_schema_rejects_undefined_execution_controls(document: dict[str, Any]) -> None:
    options = document["components"]["schemas"]["RunRequest"]["properties"]["options"]
    assert options["maxProperties"] == 0


def test_input_upload_uses_explicit_file_fields_and_artifact_id(
    document: dict[str, Any],
) -> None:
    request_body = _resolve(document, document["paths"][INPUTS_PATH]["post"]["requestBody"])
    assert request_body["required"] is True
    request = _operation_schema(document, INPUTS_PATH, "post")
    assert Draft202012Validator(request).is_valid(
        {"name": "cells.h5ad", "media_type": "application/octet-stream", "data_base64": "AQI="}
    )
    assert not Draft202012Validator(request).is_valid({"name": "cells.h5ad"})
    assert not Draft202012Validator(request).is_valid(
        {
            "name": "cells.h5ad",
            "media_type": "application/octet-stream",
            "data_base64": "AQI=",
            "object_key": "internal/storage/path",
        }
    )

    response = _operation_schema(document, INPUTS_PATH, "post", response=True)
    assert Draft202012Validator(response).is_valid({"artifact_id": "artifact-1"})
    assert not Draft202012Validator(response).is_valid({"id": "artifact-1"})
    assert not Draft202012Validator(response).is_valid(
        {"artifact_id": "artifact-1", "object_key": "internal/storage/path"}
    )


@pytest.mark.parametrize("path", [API_KEYS_PATH, PEERS_PATH])
def test_credential_creation_accepts_name_and_returns_one_time_secret(
    document: dict[str, Any], path: str
) -> None:
    request_body = _resolve(document, document["paths"][path]["post"]["requestBody"])
    assert request_body["required"] is True
    request = _operation_schema(document, path, "post")
    assert Draft202012Validator(request).is_valid({"name": "ci"})
    assert not Draft202012Validator(request).is_valid({"name": "ci", "scopes": ["runs:read"]})
    assert not Draft202012Validator(request).is_valid({})

    response = _operation_schema(document, path, "post", response=True)
    assert Draft202012Validator(response).is_valid(
        {
            "id": "credential-1",
            "name": "ci",
            "secret": "shown-once",
            "scopes": ["runs:write", "runs:read"],
        }
    )
    assert not Draft202012Validator(response).is_valid(
        {"id": "credential-1", "name": "ci", "scopes": ["runs:read", "runs:write"]}
    )
    assert not Draft202012Validator(response).is_valid(
        {
            "id": "credential-1",
            "name": "ci",
            "secret": "shown-once",
            "scopes": ["runs:read", "admin"],
        }
    )


@pytest.mark.parametrize("path", [API_KEYS_PATH, PEERS_PATH])
def test_credential_lists_are_arrays_without_secrets(
    document: dict[str, Any], path: str
) -> None:
    response = _operation_schema(document, path, "get", response=True)
    validator = Draft202012Validator(response)
    item = {"id": "credential-1", "name": "ci", "scopes": ["runs:read", "runs:write"]}
    assert validator.is_valid([{"id": "credential-1"}])
    assert validator.is_valid([item])
    assert validator.is_valid([{"id": "legacy-1", "scopes": ["lab:admin"]}])
    assert not validator.is_valid([{**item, "secret": "must-not-be-listed"}])
    assert not validator.is_valid([{**item, "api_key": "must-not-be-listed"}])


@pytest.mark.parametrize(
    ("path", "method", "operation_id", "scopes"),
    [
        (INPUTS_PATH, "post", "uploadInput", ["runs:write"]),
        (API_KEYS_PATH, "get", "listApiKeys", ["lab:admin"]),
        (API_KEYS_PATH, "post", "createApiKey", ["lab:admin"]),
        (PEERS_PATH, "get", "listPeers", ["lab:admin"]),
        (PEERS_PATH, "post", "createPeer", ["lab:admin"]),
    ],
)
def test_existing_endpoint_scopes_and_success_statuses_remain(
    document: dict[str, Any], path: str, method: str, operation_id: str, scopes: list[str]
) -> None:
    operation = document["paths"][path][method]
    assert operation["operationId"] == operation_id
    assert operation["x-required-scopes"] == scopes
    assert "200" in operation["responses"]


def test_api_key_delete_contract_remains(document: dict[str, Any]) -> None:
    delete = document["paths"][API_KEYS_PATH]["delete"]
    key_id = next(parameter for parameter in delete["parameters"] if parameter["name"] == "key_id")
    assert delete["operationId"] == "deleteApiKey"
    assert key_id["in"] == "query"
    assert key_id["required"] is True
    assert delete["x-required-scopes"] == ["lab:admin"]
    assert "204" in delete["responses"]


def test_unrelated_run_routes_keep_their_existing_scopes(document: dict[str, Any]) -> None:
    runs = document["paths"]["/v1/labs/{lab}/runs"]
    assert runs["post"]["x-required-scopes"] == ["runs:write"]
    assert runs["get"]["x-required-scopes"] == ["runs:read"]
