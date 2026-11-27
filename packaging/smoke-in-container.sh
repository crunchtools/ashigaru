#!/bin/bash
# Runs inside the smoke container (packaging/smoke.sh, packaging/rpm.Containerfile):
# install the RPMs from /rpms and run every command they ship with --help.
# REQUIRE non-empty: the `ashigaru` command must be among them.
set -euo pipefail

. /etc/os-release
if [ "$ID" != fedora ]; then
    dnf -y -q install dnf-plugins-core epel-release
    dnf config-manager --set-enabled crb
fi
dnf -y -q install $(ls /rpms/*.noarch.rpm)
commands=$(rpm -ql ashigaru ashigaru-desk | grep "^/usr/bin/")
echo "installed: $commands"
if [ -n "${REQUIRE:-}" ] && ! grep -qx /usr/bin/ashigaru <<< "$commands"; then
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
