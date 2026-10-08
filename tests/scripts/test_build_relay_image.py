"""Relay image provenance, credential boundary, and publish admission contracts."""
from pathlib import Path
import json
import subprocess
from unittest.mock import patch

import pytest

from scripts.ci import build_relay_image as release


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for args in (("init", "--quiet"), ("config", "user.name", "Image fixture"),
                 ("config", "user.email", "fixture@example.invalid"), ("config", "commit.gpgSign", "false")):
        release.git(root, *args)
    (root / "app.txt").write_text("tracked source\n", encoding="utf-8")
    release.git(root, "add", "app.txt")
    release.git(root, "commit", "--quiet", "-m", "fixture")
    return root


def test_archive_uses_git_objects_not_local_credentials(source, tmp_path):
    identity = release.source_identity(source)
    (source / ".env").write_text("SYNTHETIC_SECRET=must-not-ship\n", encoding="utf-8")
    (source / "mcp-tokens").mkdir()
    (source / "mcp-tokens/client.json").write_text("synthetic fixture, not credentials", encoding="utf-8")
    context = tmp_path / "context"
    release.tracked_context(source, context)
    assert (context / "app.txt").read_text(encoding="utf-8-sig") == "tracked source\n"
    assert not (context / ".env").exists()
    assert not (context / "mcp-tokens").exists()
    assert release.source_identity(source) == identity
    (source / "app.txt").write_text("uncommitted change\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Commit tracked"):
        release.source_identity(source)


@pytest.fixture
def receipt(tmp_path):
    data = {"commit": "a" * 40, "tree": "b" * 40, "image": release.image_reference("20261008.1234.aaaaaaaaaa.prod"),
            "platform": release.PLATFORM, "oauth_tests_passed": True, "slack_tests_passed": True,
            "docker_tests_passed": True, "published": False}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path, data


def test_receipt_requires_the_approved_commit_and_all_passing_suites(receipt):
    path, data = receipt
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(data).encode("utf-8"))
    assert release.verified_receipt(path, data["commit"]) == data
    with pytest.raises(ValueError, match="approved build"):
        release.verified_receipt(path, "c" * 40)
    for key in ("oauth_tests_passed", "slack_tests_passed", "docker_tests_passed"):
        path.write_text(json.dumps({**data, key: False}), encoding="utf-8")
        with pytest.raises(ValueError, match=key):
            release.verified_receipt(path, data["commit"])
    path.write_text(json.dumps({**data, "published": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="already been published"):
        release.verified_receipt(path, data["commit"])


def test_moving_tags_and_unexpected_repository_are_rejected(receipt):
    for tag in ("latest", "main", "prod", "bad/tag", "tag:with-colon"):
        with pytest.raises(ValueError):
            release.image_reference(tag)
    path, data = receipt
    path.write_text(json.dumps({**data, "image": "other.example/hermes:release"}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unexpected ECR"):
        release.verified_receipt(path, data["commit"])


def test_publish_preflight_distinguishes_missing_tag_and_access_denied(receipt):
    image = receipt[1]["image"]
    with patch.object(release.subprocess, "run") as checked:
        checked.return_value = subprocess.CompletedProcess([], 0, "{}", "")
        with pytest.raises(FileExistsError):
            release.ensure_unpublished(image)
        checked.return_value = subprocess.CompletedProcess([], 254, "", "ImageNotFoundException")
        release.ensure_unpublished(image)
        checked.return_value = subprocess.CompletedProcess([], 254, "", "AccessDeniedException")
        with pytest.raises(subprocess.CalledProcessError):
            release.ensure_unpublished(image)


def test_changed_archive_is_rejected_before_loading_or_publishing(receipt, tmp_path):
    path, data = receipt
    archive = tmp_path / "image.tar.gz"
    archive.write_bytes(b"original synthetic archive")
    path.write_text(json.dumps({**data, "archive_sha256": release.sha256(archive)}), encoding="utf-8")
    archive.write_bytes(b"changed synthetic archive")
    with patch.object(release, "ensure_unpublished"), patch.object(release, "run") as invoked:
        with pytest.raises(ValueError, match="checksum mismatch"):
            release.publish(path, data["commit"], archive)
        invoked.assert_not_called()


def test_image_platform_and_revision_must_match_receipt(receipt):
    data = receipt[1]
    image = {"Os": "linux", "Architecture": "arm64", "Config": {"Labels": {
        "org.opencontainers.image.revision": data["commit"], "org.opencontainers.image.source": release.SOURCE_URL}}}
    with patch.object(release, "run", return_value=json.dumps([image])) as inspected:
        release.verify_image(data["image"], data["commit"])
        with pytest.raises(ValueError, match="provenance"):
            release.verify_image(data["image"], "c" * 40)
        image["Architecture"] = "amd64"
        inspected.return_value = json.dumps([image])
        with pytest.raises(ValueError, match="linux/arm64"):
            release.verify_image(data["image"], data["commit"])
