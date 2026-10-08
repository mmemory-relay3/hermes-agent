# Relay Hermes image releases

This fork owns the Relay image build. `mmemory-relay3/relay-gitops` owns only
deployment configuration. The custom application fixes remain on
`relay/custom-patches-20261008`; the upstream-tracking `main` is not the release
source. There is no source lock or patch replay in GitOps.

## GitHub Actions

Use **Relay Hermes image** (`.github/workflows/relay-ecr-image.yml`). PRs to the
custom branch validate every source change without requesting an OIDC token.
Manual runs build the selected checkout; `publish` defaults to false.
Publication is allowed only from the custom branch in this repository.

Internal PRs and manual builds/publication use the existing ARM64
`arc-runner-dev` scale set in the shared `k8s-arc` runner group. Python 3.12 is
set up explicitly, and the publisher installs pinned AWS CLI 2.37.10 on its
ephemeral runner. External fork PRs use GitHub's native `ubuntu-24.04-arm`
runner instead.

The shared group's repository access is explicitly selected: all previously
allowed private repositories plus this public fork. Other public repositories
are not allowed, and newly created repositories require an explicit addition.
This repository's fork-PR policy requires approval for **all external
contributors**, including repeat contributors. These are GitHub settings, not
properties of the workflow YAML; keep them configured when restoring/moving CI.

The shared runner retains its existing AWS Pod Identity and privileged Docker.
Not requesting OIDC does not make its host credential-free. External PR runner
routing in YAML is not a security boundary: a proposed workflow change can
alter it. Review external PR workflow/code changes before approving any run.
The shared group's workflow access is not globally restricted, preserving the
existing private repositories' workflows. No new runner group, scale set or
shared IAM policy is created.

Set the repository variable `AWS_ECR_PUBLISH_ROLE_ARN` to:

```text
arn:aws:iam::652362962986:role/OIDC_GITHUB_ACTION_TO_ECR
```

The publisher uses existing GitHub OIDC, not a stored AWS access key. Only the
publisher has `id-token: write`; PR validation cannot request that job's token.
The shared IAM role/policy and ECR repository are not modified by this workflow.

Build identity comes from the actual checkout, not the workflow dispatch SHA
or a synthetic PR merge commit. Both the canonical install stamp and OCI
revision label identify that commit. An immutable tag contains the date,
run/attempt number and short commit, with a `.prod` suffix.

The builder uses a Git archive, excluding untracked OAuth credentials,
configuration, tokens and local environments. It preserves the official
Dockerfile and dependency locks. PM activation includes the Slack extra, and
all tests use `scripts/run_tests.sh` with isolated homes and no file retries.
OAuth, Slack, build admission and real Docker integration tests must pass.

The build job exports its tested image as a checksummed gzip archive. The
separate publisher verifies the receipt, archive, architecture and OCI
provenance, then pushes that image without rebuilding. Existing ECR tags are
rejected. `relay-hermes-published-receipt` records the image, commit/tree, test
results and registry digest. Review ECR scan findings before deployment;
functional tests do not approve vulnerabilities, and this change does not
remediate or change their acceptance policy.

## Local equivalent

Use a clean committed checkout, Docker/Buildx, Python 3.11+, and a fresh receipt
directory. Prepare dependencies with `source ./activate --test-extras all,slack --`
or select an existing PM-built Slack/test interpreter with `HERMES_PYTHON`.
If activated, select its test interpreter explicitly and clear the inherited
activation before invoking the isolated builder:

```bash
if [ -n "${__HERMES_TEST_PYTHON:-}" ]; then
  export HERMES_PYTHON="$__HERMES_TEST_PYTHON"
fi
unset __HERMES_ACTIVATED
relay_release_dir=$(mktemp -d)
relay_commit=$(git rev-parse HEAD)
relay_tag="$(date -u +%Y%m%d.%H%M%S).${relay_commit:0:10}.prod"
python3 scripts/ci/build_relay_image.py build \
  --tag "$relay_tag" --receipt "$relay_release_dir/receipt.json"
# AWS credentials are required only for this separate publication step.
python3 scripts/ci/build_relay_image.py publish \
  --expected-commit "$relay_commit" --receipt "$relay_release_dir/receipt.json"
```

Do not commit the generated receipt or image archive. No Google OAuth client,
Slack token, dashboard password or runtime home is needed to build.

## GitOps handoff and rollback

After publication, verify that the receipt is `published: true` and retain its
digest/Actions link in the deployment PR. Change only `image.tag` in the
GitOps `dev/apps/relay-hermes/values.yaml` and mirrored prod values. The dev
Application remains retired. Merge the reviewed GitOps PR, then separately
authorize a manual prod Argo CD sync. This workflow never writes to GitOps,
merges a PR, syncs Argo CD or starts/stops application workloads. ARC creates
and removes ephemeral CI runner Pods automatically.

Preserve the existing PVC, Secret/ExternalSecret and `bootstrap.overwrite: false`.
OAuth tokens and MCP settings live in the runtime PVC, not this image. Validate
dashboard authentication, Slack status/clearing and Drive/Calendar/Relay MCP
after rollout. Rollback changes the values tag to the previous published image
and performs a separately approved manual sync, retaining the same data volume.
