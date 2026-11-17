#!/bin/bash
# Runs inside the build container (packaging/build.sh): install the build tools
# and the spec's dependencies, build, and leave the RPMs in /out.
set -euo pipefail

. /etc/os-release
if [ "$ID" != fedora ]; then
    # EL: the Python stack is in CRB and EPEL.
    dnf -y -q install dnf-plugins-core epel-release
    dnf config-manager --set-enabled crb
fi
dnf -y -q install rpm-build rpmdevtools pyproject-rpm-macros python3-devel

top=$(mktemp -d)
mkdir -p "$top/SOURCES" "$top/SPECS"
cp "/out/ashigaru-$ASHIGARU_VERSION.tar.gz" "$top/SOURCES/"
cp /packaging/ashigaru.spec "$top/SPECS/"
define=(--define "_topdir $top" --define "ashigaru_version $ASHIGARU_VERSION")

# %generate_buildrequires: build the source RPM with the dynamic build
# dependencies, install them, and repeat until nothing new is asked for.
for _ in 1 2 3 4 5; do
    if rpmbuild -br "${define[@]}" "$top/SPECS/ashigaru.spec" > "$top/br.log" 2>&1; then
        break
    fi
    srpm=$(sed -n 's/^Wrote: \(.*buildreqs.nosrc.rpm\)$/\1/p' "$top/br.log" | tail -1)
    [ -n "$srpm" ] || { cat "$top/br.log"; exit 1; }
    dnf -y -q builddep "$srpm"
done

rpmbuild -ba "${define[@]}" "$top/SPECS/ashigaru.spec"
find "$top/RPMS" "$top/SRPMS" -name '*.rpm' ! -name '*.buildreqs.nosrc.rpm' -exec cp {} /out/ \;
ls -1 /out/*.rpm
