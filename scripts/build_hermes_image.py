#!/usr/bin/env python3
"""Build a local Hermes image from the pinned upstream commit and patch."""

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


PIN_PATH = Path(__file__).resolve().parents[1] / "vendor/hermes/PIN.json"
UPSTREAM_TESTS = (
    "tests/gateway/test_api_server_run_idempotency.py",
    "tests/gateway/test_api_server_runs.py",
    "tests/tools/test_approval_gateway_wait_late_choice.py",
)


def verify_source_pin(source_repo: Path, expected_commit: str) -> None:
    actual = subprocess.check_output(
        ["git", "-C", str(source_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != expected_commit:
        raise ValueError(f"source commit mismatch: expected {expected_commit}, got {actual}")


def verify_patch_digest(patch: Path, expected_sha256: str) -> None:
    actual = hashlib.sha256(patch.read_bytes()).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"patch digest mismatch: expected {expected_sha256}, got {actual}")


def build_local_image(context: Path, tag: str) -> str:
    image_id_file = context.parent / "image-id"
    subprocess.run(
        ["docker", "build", "--iidfile", str(image_id_file), "-t", tag, str(context)],
        check=True,
    )
    return image_id_file.read_text().strip()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_repo", type=Path)
    parser.add_argument("image_tag")
    args = parser.parse_args(argv)
    args.source_repo = args.source_repo.resolve()
    pin = json.loads(PIN_PATH.read_text())
    commit = pin["source_commit"]
    digest = pin["patch_sha256"]
    patch = PIN_PATH.parent / pin["patch_file"]
    verify_source_pin(args.source_repo, commit)
    verify_patch_digest(patch, digest)

    with tempfile.TemporaryDirectory(prefix="hermes-image-") as temporary:
        root = Path(temporary)
        archive = root / "source.tar"
        context = root / "context"
        context.mkdir()
        with archive.open("wb") as output:
            subprocess.run(
                ["git", "-C", str(args.source_repo), "archive", "--format=tar", commit],
                stdout=output,
                check=True,
            )
        with tarfile.open(archive) as source:
            source.extractall(context, filter="data")
        if not (context / "Dockerfile").is_file():
            raise ValueError("pinned source has no root Dockerfile")
        try:
            subprocess.run(["git", "apply", "--check", str(patch)], cwd=context, check=True, capture_output=True)
        except subprocess.CalledProcessError as error:
            raise ValueError("patch does not apply to pinned source") from error
        subprocess.run(["git", "apply", str(patch)], cwd=context, check=True)
        upstream_python = args.source_repo / ".venv/bin/python"
        test_python = upstream_python if upstream_python.is_file() else sys.executable
        subprocess.run([str(test_python), "-m", "pytest", "-q", *UPSTREAM_TESTS], cwd=context, check=True)
        image_id = build_local_image(context, args.image_tag)

    print(f"source commit: {commit}")
    print(f"patch sha256: {digest}")
    print(f"local image ID: {image_id}")


if __name__ == "__main__":
    main()
