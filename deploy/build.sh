#!/usr/bin/env bash
# Build the Linux binary that the runtime image ships.
#
# Run this on a build machine that has Go and bun. The produced image itself
# never needs them: Dockerfile only copies bin/new-api plus OS runtime libs.
#
# Usage:
#   bash build.sh              # frontend + linux/amd64 and linux/arm64 binaries
#   bash build.sh --image      # ... and build the docker image too
#   ARCH=arm64 bash build.sh   # only that architecture
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../new-api" && pwd)"
VERSION="${VERSION:-community-1.0.0}"
# Default: both, so one bundle covers x86_64 and ARM servers alike.
TARGETS="${ARCH:-amd64 arm64}"

say() { printf '\033[36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v go >/dev/null 2>&1 || die "go not found in PATH"
command -v bun >/dev/null 2>&1 || die "bun not found in PATH"

say "building frontend"
cd "$REPO/web"
bun install --frozen-lockfile
bun run build
[ -f dist/index.html ] || die "web/dist/index.html missing after build"

# Build every requested architecture. deploy.sh picks the matching one at
# install time, so a single bundle works on both x86 and ARM servers.
mkdir -p "$HERE/bin"
cd "$REPO"
for target in $TARGETS; do
  say "building linux/$target binary"
  GOOS=linux GOARCH="$target" CGO_ENABLED=0 \
    go build -trimpath \
    -ldflags "-s -w -X 'github.com/QuantumNous/new-api/common.Version=$VERSION'" \
    -o "$HERE/bin/new-api-$target" .
  chmod 0755 "$HERE/bin/new-api-$target"
done

# Keep an un-suffixed copy for local `docker build` on this machine.
HOST_ARCH="$(go env GOARCH)"
if [ -f "$HERE/bin/new-api-$HOST_ARCH" ]; then
  cp "$HERE/bin/new-api-$HOST_ARCH" "$HERE/bin/new-api"
  chmod 0755 "$HERE/bin/new-api"
fi

say "collecting license files"
mkdir -p "$HERE/licenses"
for f in LICENSE NOTICE THIRD-PARTY-LICENSES.md; do
  [ -f "$REPO/$f" ] && cp "$REPO/$f" "$HERE/licenses/"
done

say "artifacts"
ls -lh "$HERE/bin/"new-api*

if [ "${1:-}" = "--image" ]; then
  command -v docker >/dev/null 2>&1 || die "docker not found in PATH"
  say "building docker image community-gateway:latest"
  docker build -t community-gateway:latest "$HERE"
  docker image inspect community-gateway:latest --format 'image size: {{.Size}} bytes'
fi

say "done"
