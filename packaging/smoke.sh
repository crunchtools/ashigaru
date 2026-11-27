#!/bin/bash
# Install the built RPMs in a fresh container and run every console script
# they ship with --help, and check the /ashigaru skill is installed.
#   packaging/smoke.sh <image> <rpmdir> [require-cli]
# With require-cli (a release), the `ashigaru` command must be among them.
set -euo pipefail

image=$1
rpms=$(realpath "$2")
require=${3:-}

podman run --rm -v "$rpms:/rpms:ro,Z" -v "$(dirname "$(realpath "$0")"):/packaging:ro,Z" \
    -e REQUIRE="$require" "$image" bash /packaging/smoke-in-container.sh
