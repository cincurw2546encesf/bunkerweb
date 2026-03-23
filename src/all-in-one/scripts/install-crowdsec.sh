#!/bin/bash

set -euo pipefail
# shellcheck disable=SC1091
. "$(dirname "$0")/utils.sh"

echo "ℹ️ Cloning and building CrowdSec $VERSION"

echo "ℹ️ Cloning CrowdSec from $URL (commit $COMMIT)"
git_clone_commit crowdsec "$URL" "$COMMIT"

echo "ℹ️ Patching CrowdSec Go dependencies for CVE fixes"
go get google.golang.org/grpc@v1.79.3         # CVE-2026-33186
go get github.com/buger/jsonparser@v1.1.2      # GHSA-6g7g-w4f8-9c9x
go get golang.org/x/crypto@v0.46.0             # CVE-2025-47913
go mod tidy

echo "ℹ️ Building CrowdSec"
make clean release BUILD_VERSION="$VERSION" DOCKER_BUILD=1 BUILD_STATIC=1 CGO_CFLAGS="-D_LARGEFILE64_SOURCE"

echo "✅ CrowdSec $VERSION built successfully"
