#!/usr/bin/env python3
"""Build/test Relay's checked-out Hermes source, then publish that exact image."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

REPOSITORY = "652362962986.dkr.ecr.ap-northeast-2.amazonaws.com/relay-hermes"
REGION = "ap-northeast-2"
PLATFORM = "linux/arm64"
SOURCE_URL = "https://github.com/mmemory-relay3/hermes-agent"
ROOT = Path(__file__).resolve().parents[2]
OAUTH_TESTS = (
    "tests/tools/test_mcp_oauth*.py", "tests/tools/test_mcp_dashboard_oauth.py",
    "tests/hermes_cli/test_mcp*oauth*.py", "tests/hermes_cli/test_mcp_login*.py",
    "tests/tui_gateway/test_mcp_oauth*.py",
)
SLACK_TESTS = (
    "tests/plugins/test_slack_status_api.py", "tests/gateway/test_slack.py",
    "tests/gateway/test_slack_status_update.py", "tests/gateway/test_slack_native_streaming.py",
    "tests/gateway/test_slack_turn_recipient_identity.py",
)
DOCKER_TESTS = (
    "tests/docker/test_smoke.py", "tests/docker/test_dashboard.py",
    "tests/docker/test_dump_build_sha.py", "tests/docker/test_image_payload.py",
    "tests/docker/test_sqlite_runtime.py", "tests/docker/test_immutable_install.py",
)


def run(args, *, cwd=None, env=None, capture=False, input=None):
    return subprocess.run([str(arg) for arg in args], cwd=cwd, env=env, input=input,
        check=True, text=True, stdout=subprocess.PIPE if capture else None).stdout


def git(source, *args):
    return run(["git", "-C", source, *args], capture=True).strip()


def source_identity(source):
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Commit tracked source changes before building a release")
    return {"commit": git(source, "rev-parse", "HEAD"),
            "tree": git(source, "rev-parse", "HEAD^{tree}")}


def image_reference(tag):
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) or tag in {"latest", "main", "dev", "prod"}:
        raise ValueError("Use a unique immutable release tag, not a moving channel")
    return REPOSITORY + ":" + tag


def tracked_context(source, destination):
    """Export Git objects; untracked credentials and local virtualenvs stay out."""
    destination.mkdir()
    archive = subprocess.Popen(["git", "-C", str(source), "archive", "--format=tar", "HEAD"],
                               stdout=subprocess.PIPE)
    try:
        subprocess.run(["tar", "-x", "-C", str(destination)], stdin=archive.stdout, check=True)
    finally:
        archive.stdout.close()
        if archive.wait() != 0:
            raise RuntimeError("Git archive failed")


def test_paths(source, patterns):
    paths = sorted({str(path.relative_to(source)) for pattern in patterns for path in source.glob(pattern)})
    if not paths:
        raise ValueError("Required regression test suite is missing")
    return paths


def verify_image(image, commit):
    inspected = json.loads(run(["docker", "image", "inspect", image], capture=True))[0]
    if inspected["Os"] != "linux" or inspected["Architecture"] != "arm64":
        raise ValueError("Expected a linux/arm64 image")
    labels = inspected["Config"].get("Labels") or {}
    if labels.get("org.opencontainers.image.revision") != commit or labels.get("org.opencontainers.image.source") != SOURCE_URL:
        raise ValueError("Image provenance does not match the tested fork commit")


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def export_image(image, archive):
    archive.parent.mkdir(parents=True, exist_ok=True)
    saved = subprocess.Popen(["docker", "save", image], stdout=subprocess.PIPE)
    try:
        with gzip.open(archive, "wb") as output:
            shutil.copyfileobj(saved.stdout, output)
    finally:
        saved.stdout.close()
        if saved.wait() != 0:
            raise RuntimeError("Docker image export failed")
    return sha256(archive)


def build(source, tag, receipt, archive=None):
    source = source.resolve()
    identity = source_identity(source)
    image = image_reference(tag)
    expected_stamp = source / "install-stamp.json"
    if expected_stamp.exists() or expected_stamp.is_symlink():
        raise FileExistsError("Use a checkout without an existing install-stamp.json")
    with tempfile.TemporaryDirectory(prefix="relay-hermes-build-") as temporary:
        workspace = Path(temporary)
        env = {**os.environ, "HERMES_TEST_FILE_RETRIES": "0", "HERMES_TEST_WORKERS": "2",
               "HERMES_HOME": str(workspace / "test-home"), "HERMES_RUNTIME_DIR": str(workspace / "test-runtime")}
        paths = test_paths(source, OAUTH_TESTS) + test_paths(source, SLACK_TESTS)
        paths += test_paths(source, ("tests/scripts/test_build_relay_image.py",))
        run(["bash", "scripts/run_tests.sh", *sorted(set(paths)), "--tb=short"], cwd=source, env=env)
        if source_identity(source) != identity:
            raise ValueError("Source changed during regression tests")
        context = workspace / "context"
        tracked_context(source, context)
        stamp_args = [sys.executable, "scripts/write_install_stamp.py", "--output", context / "install-stamp.json",
            "--commit", identity["commit"], "--branch", git(source, "rev-parse", "--abbrev-ref", "HEAD"),
            "--distribution", "docker", "--update-mechanism", "external", "--source", "ci"]
        display = run([sys.executable, "-m", "scripts.releases.distance"], cwd=source, capture=True).strip()
        if display:
            base, separator, development = display.partition("+")
            stamp_args += ["--base-version", base, "--display-version", display,
                           "--distance", development.split(".", 1)[0] if separator else "0"]
        run(stamp_args, cwd=source)
        run([sys.executable, "scripts/ci/check_profile_archive_boundary.py", "--root", context], cwd=source)
        run(["docker", "buildx", "build", "--platform", PLATFORM, "--load", "--progress", "plain", "--tag", image,
             "--label", "org.opencontainers.image.revision=" + identity["commit"],
             "--label", "org.opencontainers.image.source=" + SOURCE_URL, "--file", context / "Dockerfile", context])
        # Upstream Docker tests compare the image stamp to this independent input.
        expected_stamp.write_bytes((context / "install-stamp.json").read_bytes())
        try:
            run(["bash", "scripts/run_tests.sh", *DOCKER_TESTS, "--tb=short"], cwd=source,
                env={**env, "HERMES_TEST_IMAGE": image})
        finally:
            expected_stamp.unlink()
        if source_identity(source) != identity:
            raise ValueError("Source changed during Docker tests")
        verify_image(image, identity["commit"])
        result = {**identity, "image": image, "platform": PLATFORM,
                  "oauth_tests_passed": True, "slack_tests_passed": True,
                  "docker_tests_passed": True, "published": False}
        if archive is not None:
            result["archive_sha256"] = export_image(image, archive)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(result, indent=2) + "\n")
        return result


def verified_receipt(receipt, expected_commit):
    result = json.loads(receipt.read_text())
    if not re.fullmatch(r"[a-f0-9]{40}", expected_commit) or result.get("commit") != expected_commit:
        raise ValueError("Receipt commit differs from the approved build")
    if not re.fullmatch(r"[a-f0-9]{40}", result.get("tree", "")) or result.get("platform") != PLATFORM:
        raise ValueError("Invalid source tree or platform in receipt")
    for key in ("oauth_tests_passed", "slack_tests_passed", "docker_tests_passed"):
        if result.get(key) is not True:
            raise ValueError("Cannot publish without passing " + key)
    if result.get("published") is not False:
        raise ValueError("This receipt has already been published")
    prefix = REPOSITORY + ":"
    if not result.get("image", "").startswith(prefix) or image_reference(result["image"][len(prefix):]) != result["image"]:
        raise ValueError("Unexpected ECR image reference")
    return result


def ensure_unpublished(image):
    tag = image.rsplit(":", 1)[1]
    args = ["aws", "ecr", "describe-images", "--region", REGION, "--repository-name", "relay-hermes",
            "--image-ids", "imageTag=" + tag, "--output", "json"]
    checked = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if checked.returncode == 0:
        raise FileExistsError("ECR tag already exists; choose a new immutable release tag")
    if "ImageNotFoundException" not in checked.stderr:
        raise subprocess.CalledProcessError(checked.returncode, args, output=checked.stdout, stderr=checked.stderr)


def publish(receipt, expected_commit, archive=None):
    result = verified_receipt(receipt, expected_commit)
    ensure_unpublished(result["image"])
    if archive is not None:
        if result.get("archive_sha256") != sha256(archive):
            raise ValueError("Tested image archive checksum mismatch")
        run(["docker", "load", "--input", archive])
    verify_image(result["image"], expected_commit)
    token = run(["aws", "ecr", "get-login-password", "--region", REGION], capture=True)
    run(["docker", "login", "--username", "AWS", "--password-stdin", REPOSITORY.split("/", 1)[0]], input=token)
    run(["docker", "push", result["image"]])
    details = json.loads(run(["aws", "ecr", "describe-images", "--region", REGION,
        "--repository-name", "relay-hermes", "--image-ids", "imageTag=" + result["image"].rsplit(":", 1)[1],
        "--query", "imageDetails[0]", "--output", "json"], capture=True))
    result.update(published=True, digest=details["imageDigest"], size_bytes=details["imageSizeInBytes"])
    receipt.write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    builder = commands.add_parser("build", help="Test/build without AWS credentials")
    builder.add_argument("--source", type=Path, default=ROOT)
    builder.add_argument("--tag", required=True)
    builder.add_argument("--receipt", type=Path, required=True)
    builder.add_argument("--archive", type=Path, help="Optional gzip image archive for a separate publisher")
    publisher = commands.add_parser("publish", help="Publish an already-tested image, never rebuild it")
    publisher.add_argument("--receipt", type=Path, required=True)
    publisher.add_argument("--expected-commit", required=True)
    publisher.add_argument("--archive", type=Path)
    args = parser.parse_args()
    result = build(args.source, args.tag, args.receipt, args.archive) if args.operation == "build" else publish(args.receipt, args.expected_commit, args.archive)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
