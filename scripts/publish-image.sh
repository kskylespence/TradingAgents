#!/usr/bin/env bash
# Build the Coolify image on this machine and push it to GHCR, instead of
# running the "Publish Docker image" workflow (which spends Actions minutes).
#
# Only needed when Coolify pulls a prebuilt image (DEPLOY.md, "Prebuilt image").
# By default Coolify builds the Dockerfile from git on the VPS, and nothing
# here is needed.
#
# Needs: Docker Desktop running, and a one-time `docker login ghcr.io` with a
# GitHub token that has write:packages. HEAD must be a clean, tagged release
# commit (v<base>-hf.<n>, docs/RELEASING.md section 1).
#
# Usage: scripts/publish-image.sh
#   PLATFORM=linux/arm64 scripts/publish-image.sh   # an ARM VPS (default amd64)

set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

IMAGE=${IMAGE:-ghcr.io/kskylespence/tradingagents}
PLATFORM=${PLATFORM:-linux/amd64}

if [ -n "$(git status --porcelain)" ]; then
  echo "publish-image: the working tree has uncommitted changes; the image would not match the tag." >&2
  exit 1
fi
tag=$(git describe --exact-match --tags --match 'v*-hf.*' HEAD 2>/dev/null) || {
  echo "publish-image: HEAD is not a v*-hf.* release tag. Cut one first (docs/RELEASING.md section 1)." >&2
  exit 1
}
version=${tag#v}   # v0.7.1-hf.1 -> 0.7.1-hf.1, as the workflow tags it

echo "publish-image: building $IMAGE:$version and :latest for $PLATFORM"
docker buildx build --platform "$PLATFORM" \
  --tag "$IMAGE:$version" --tag "$IMAGE:latest" \
  --push .

# The registry, not the build's exit code, is the proof the push landed.
docker buildx imagetools inspect "$IMAGE:$version" >/dev/null
echo "publish-image: $IMAGE:$version is in the registry"
