import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "build_hermes_image.py"
spec = importlib.util.spec_from_file_location("build_hermes_image", SCRIPT)
build_hermes_image = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build_hermes_image)


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Test")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / "LICENSE").write_text("MIT fixture\n")
    (repo / "README").write_text("before\n")
    for path in (
        "tests/gateway/test_api_server_run_idempotency.py",
        "tests/gateway/test_api_server_runs.py",
        "tests/tools/test_approval_gateway_wait_late_choice.py",
    ):
        test_file = repo / path
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("def test_upstream_gate():\n    assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    commit = git(repo, "rev-parse", "HEAD")
    (repo / "README").write_text("after\n")
    patch = tmp_path / "approval.patch"
    patch.write_bytes(subprocess.check_output(["git", "-C", str(repo), "diff", "--", "README"]))
    pin = tmp_path / "PIN.json"
    pin.write_text(json.dumps({"source_commit": commit, "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(), "patch_file": patch.name}))
    monkeypatch.setattr(build_hermes_image, "PIN_PATH", pin)
    return repo, pin, patch, commit


def test_wrong_source_commit_stops_before_docker(source, monkeypatch):
    repo, pin, _, _ = source
    data = json.loads(pin.read_text())
    data["source_commit"] = "0" * 40
    pin.write_text(json.dumps(data))
    monkeypatch.setattr(build_hermes_image, "build_local_image", lambda *_: pytest.fail("Docker started"))
    with pytest.raises(ValueError, match="source commit mismatch"):
        build_hermes_image.main([str(repo), "local:fixture"])


def test_changed_patch_stops_before_docker(source, monkeypatch):
    repo, _, patch, _ = source
    patch.write_bytes(patch.read_bytes() + b"\n")
    monkeypatch.setattr(build_hermes_image, "build_local_image", lambda *_: pytest.fail("Docker started"))
    with pytest.raises(ValueError, match="patch digest mismatch"):
        build_hermes_image.main([str(repo), "local:fixture"])


def test_unapplicable_patch_stops_before_docker(source, monkeypatch):
    repo, pin, patch, _ = source
    patch.write_text("invalid patch\n")
    data = json.loads(pin.read_text())
    data["patch_sha256"] = hashlib.sha256(patch.read_bytes()).hexdigest()
    pin.write_text(json.dumps(data))
    monkeypatch.setattr(build_hermes_image, "build_local_image", lambda *_: pytest.fail("Docker started"))
    with pytest.raises(ValueError, match="patch does not apply"):
        build_hermes_image.main([str(repo), "local:fixture"])


def test_failing_upstream_test_stops_before_docker(source, monkeypatch):
    repo, pin, _, _ = source
    (repo / "tests/gateway/test_api_server_runs.py").write_text(
        "def test_upstream_gate():\n    assert False\n"
    )
    git(repo, "add", "tests/gateway/test_api_server_runs.py")
    git(repo, "commit", "-qm", "failing upstream test")
    data = json.loads(pin.read_text())
    data["source_commit"] = git(repo, "rev-parse", "HEAD")
    pin.write_text(json.dumps(data))
    monkeypatch.setattr(build_hermes_image, "build_local_image", lambda *_: pytest.fail("Docker started"))
    with pytest.raises(subprocess.CalledProcessError):
        build_hermes_image.main([str(repo), "local:fixture"])


def test_build_uses_patched_commit_export_and_reports_local_id(source, monkeypatch, capsys):
    repo, _, _, commit = source

    def fake_build(context: Path, tag: str) -> str:
        assert tag == "local:fixture"
        assert (context / "README").read_text() == "after\n"
        assert (context / "LICENSE").read_text() == "MIT fixture\n"
        assert (context / "Dockerfile").is_file()
        assert not (context / ".git").exists()
        return "sha256:" + "1" * 64

    monkeypatch.setattr(build_hermes_image, "build_local_image", fake_build)
    build_hermes_image.main([str(repo), "local:fixture"])
    output = capsys.readouterr().out
    assert commit in output
    assert "local image ID: sha256:" + "1" * 64 in output


def test_pin_and_digest_helpers(source):
    repo, pin, patch, commit = source
    build_hermes_image.verify_source_pin(repo, commit)
    build_hermes_image.verify_patch_digest(patch, json.loads(pin.read_text())["patch_sha256"])
    with pytest.raises(ValueError, match="source commit mismatch"):
        build_hermes_image.verify_source_pin(repo, "0" * 40)


def test_docker_build_records_local_image_id(tmp_path, monkeypatch):
    context = tmp_path / "context"
    context.mkdir()

    def fake_docker(command, *, check):
        assert command == ["docker", "build", "--iidfile", str(tmp_path / "image-id"), "-t", "local:fixture", str(context)]
        assert check is True
        (tmp_path / "image-id").write_text("sha256:" + "2" * 64 + "\n")

    monkeypatch.setattr(build_hermes_image.subprocess, "run", fake_docker)
    assert build_hermes_image.build_local_image(context, "local:fixture") == "sha256:" + "2" * 64
