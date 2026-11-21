#!/bin/bash
# Build the ashigaru RPMs from this checkout inside a fresh container.
#   packaging/build.sh <image> <outdir> [tag]
# <image> is the build root (fedora:43, fedora:45, centos:stream10, ...). The version is
# VERSION's; given a release tag, the build refuses unless the tag names it.
set -euo pipefail

image=$1
out=$(realpath -m "$2")
root=$(git rev-parse --show-toplevel)
version=$(tr -d '[:space:]' < "$root/VERSION")
tag=${3:-}
if [ -n "$tag" ] && [ "$tag" != "v$version" ]; then
    echo "tag $tag does not match VERSION $version" >&2
    exit 1
fi
mkdir -p "$out"

git -C "$root" archive --format=tar.gz --prefix="ashigaru-$version/" \
    -o "$out/ashigaru-$version.tar.gz" HEAD

podman run --rm \
    -v "$root/packaging:/packaging:ro,Z" -v "$out:/out:Z" \
    -e ASHIGARU_VERSION="$version" "$image" bash /packaging/rpmbuild-in-container.sh
