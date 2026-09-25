from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit


class S3ArtifactStorage:
    def __init__(self, client: Any, *, bucket: str) -> None:
        if not bucket.strip():
            raise ValueError("bucket must be non-blank")
        self.client = client
        self.bucket = bucket

    @staticmethod
    def _uri_parts(uri: str) -> tuple[str, str, str] | None:
        if not isinstance(uri, str) or not uri or "?" in uri or "#" in uri:
            return None
        try:
            parsed = urlsplit(uri)
            path = unquote(parsed.path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None
        if (
            parsed.scheme.lower() != "s3"
            or not parsed.netloc
            or any(character in parsed.netloc for character in "@:%")
            or not path.startswith("/")
            or path.startswith("//")
        ):
            return None
        segments = path[1:].split("/")
        if len(segments) < 2 or any(
            not segment
            or segment in {".", ".."}
            or "\\" in segment
            or "\x00" in segment
            for segment in segments
        ):
            return None
        lab_id, run_id = parsed.netloc, segments[0]
        return lab_id, run_id, f"{lab_id}/{'/'.join(segments)}"

    def owns(self, uri: str, *, lab_id: str, run_id: str) -> bool:
        parts = self._uri_parts(uri)
        return parts is not None and parts[:2] == (lab_id, run_id)

    @classmethod
    def _object_key(cls, uri: str) -> str:
        parts = cls._uri_parts(uri)
        if parts is None:
            raise ValueError("artifact URI must be a safe s3://<lab>/<run>/<object> URI")
        return parts[2]

    def put(
        self,
        uri: str,
        content: bytes,
        *,
        metadata: Mapping[str, Any],
        if_none_match: bool = True,
    ) -> None:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": self._object_key(uri),
            "Body": content,
            "Metadata": {str(key): str(value) for key, value in metadata.items()},
        }
        if if_none_match:
            request["IfNoneMatch"] = "*"
        self.client.put_object(**request)

    def read_bytes(self, uri: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(uri))
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def presign(self, uri: str, *, expires_in: int = 900) -> str:
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": self._object_key(uri)},
            ExpiresIn=expires_in,
        )
