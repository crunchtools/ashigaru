#!/bin/bash
# Install the built RPMs in a fresh container and run every console script
# they ship with --help, and check the /ashigaru skill is installed.
#   packaging/smoke.sh <image> <rpmdir> [require-cli]
# With require-cli (a release), the `ashigaru` command must be among them.
set -euo pipefail

image=$1
rpms=$(realpath "$2")
require=${3:-}

podman run --rm -v "$rpms:/rpms:ro,Z" -e REQUIRE="$require" "$image" bash -euo pipefail -c '
    . /etc/os-release
    if [ "$ID" != fedora ]; then
        dnf -y -q install dnf-plugins-core epel-release
        dnf config-manager --set-enabled crb
    fi
    dnf -y -q install $(ls /rpms/*.noarch.rpm)
    commands=$(rpm -ql ashigaru ashigaru-desk | grep "^/usr/bin/")
    echo "installed: $commands"
    if [ -n "$REQUIRE" ] && ! grep -qx /usr/bin/ashigaru <<< "$commands"; then
        echo "no /usr/bin/ashigaru in the package" >&2
        exit 1
    fi
    skill=/usr/share/ashigaru/skills/ashigaru/SKILL.md
    rpm -ql ashigaru | grep -qx "$skill" || { echo "no $skill in the package" >&2; exit 1; }
    head -2 "$skill" | grep -qx "name: ashigaru"
    echo "installed: $skill"
    for command in $commands; do
        "$command" --help > /dev/null
        echo "ok: $command --help"
    done
'
