from io import BytesIO

import pytest

from scilab.infra.s3_artifacts import S3ArtifactStorage


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.put_calls: list[dict[str, object]] = []
        self.get_calls: list[dict[str, object]] = []
        self.presign_calls: list[tuple[str, dict[str, object], int]] = []

    def put_object(self, **request: object) -> None:
        self.put_calls.append(request)
        key = (str(request["Bucket"]), str(request["Key"]))
        if request.get("IfNoneMatch") == "*" and key in self.objects:
            raise RuntimeError("object already exists")
        self.objects[key] = request["Body"]  # type: ignore[assignment]

    def get_object(self, **request: object) -> dict[str, BytesIO]:
        self.get_calls.append(request)
        key = (str(request["Bucket"]), str(request["Key"]))
        return {"Body": BytesIO(self.objects[key])}

    def generate_presigned_url(
        self,
        operation: str,
        *,
        Params: dict[str, object],
        ExpiresIn: int,
    ) -> str:
        self.presign_calls.append((operation, Params, ExpiresIn))
        return "https://minio.example/presigned"


def test_owns_requires_exact_lab_run_and_safe_object_path() -> None:
    storage = S3ArtifactStorage(object(), bucket="artifacts")

    assert storage.owns(
        "s3://lab-a/run-1/results/data.bin", lab_id="lab-a", run_id="run-1"
    )
    assert not storage.owns(
        "s3://lab-b/run-1/results/data.bin", lab_id="lab-a", run_id="run-1"
    )
    assert not storage.owns(
        "s3://lab-a/run-10/results/data.bin", lab_id="lab-a", run_id="run-1"
    )
    assert not storage.owns(
        "s3://lab-a/run-1/../../lab-b/run-2/stolen.bin",
        lab_id="lab-a",
        run_id="run-1",
    )


def test_put_is_create_only_by_default() -> None:
    client = FakeS3Client()
    storage = S3ArtifactStorage(client, bucket="artifacts")
    uri = "s3://lab-a/run-1/results/data.bin"

    storage.put(uri, b"\x00\xff", metadata={"media_type": "application/octet-stream"})

    assert client.put_calls == [
        {
            "Bucket": "artifacts",
            "Key": "lab-a/run-1/results/data.bin",
            "Body": b"\x00\xff",
            "Metadata": {"media_type": "application/octet-stream"},
            "IfNoneMatch": "*",
        }
    ]
    with pytest.raises(RuntimeError, match="object already exists"):
        storage.put(uri, b"replacement", metadata={})
    assert client.objects[("artifacts", "lab-a/run-1/results/data.bin")] == b"\x00\xff"


def test_read_bytes_preserves_binary_and_presign_defaults_to_15_minutes() -> None:
    client = FakeS3Client()
    storage = S3ArtifactStorage(client, bucket="artifacts")
    key = "lab-a/run-1/results/data.bin"
    client.objects[("artifacts", key)] = b"\x00\xff\x80binary"

    assert storage.read_bytes(f"s3://{key}") == b"\x00\xff\x80binary"
    assert storage.presign(f"s3://{key}") == "https://minio.example/presigned"
    assert client.get_calls == [{"Bucket": "artifacts", "Key": key}]
    assert client.presign_calls == [
        ("get_object", {"Bucket": "artifacts", "Key": key}, 900)
    ]


def test_read_rejects_traversal_before_requesting_s3() -> None:
    client = FakeS3Client()
    storage = S3ArtifactStorage(client, bucket="artifacts")

    with pytest.raises(ValueError, match="safe s3://"):
        storage.read_bytes("s3://lab-a/run-1/../run-2/stolen.bin")
    assert client.get_calls == []


def test_s3_client_failure_is_propagated() -> None:
    failure = OSError("object store unavailable")

    class FailingClient:
        def get_object(self, **request: object) -> dict[str, object]:
            raise failure

    storage = S3ArtifactStorage(FailingClient(), bucket="artifacts")

    with pytest.raises(OSError) as raised:
        storage.read_bytes("s3://lab-a/run-1/results/data.bin")
    assert raised.value is failure
    assert not storage.owns(
        "s3://lab-a/run-1/%2e%2e/run-2/stolen.bin",
        lab_id="lab-a",
        run_id="run-1",
    )
