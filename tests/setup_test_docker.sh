#!/usr/bin/env bash
# Build the local test image without moving files around.
# This script can be run from any working directory.
set -euo pipefail

# Resolve repository root relative to this script's location
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." >/dev/null 2>&1 && pwd)"

DOCKERFILE="${ROOT_DIR}/tests/swe_rex_test.Dockerfile"
BUILD_CONTEXT="${ROOT_DIR}"

# TARGETARCH should be set automatically on most (but not all) systems.
# Fall back to the current machine architecture.
TARGETARCH="$(uname -m)"

# Build the test image using the Dockerfile in tests/ and the repo root as context.
docker build \
  -t swe-rex-test:latest \
  -f "${DOCKERFILE}" \
  --build-arg TARGETARCH="${TARGETARCH}" \
  "${BUILD_CONTEXT}"
