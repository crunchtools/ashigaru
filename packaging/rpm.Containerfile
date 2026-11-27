# Build the ashigaru RPMs on a distro and smoke-test them in a fresh one: the
# pull-request half of .github/workflows/rpm.yml, run by sdlc ci
# ([tool.sdlc.ci.rpm-*] in pyproject.toml). Nothing is published from here.
#   podman build -f packaging/rpm.Containerfile --build-arg TARGET=fc43 .
# The build is packaging/rpmbuild-in-container.sh and the smoke test
# packaging/smoke-in-container.sh, the same scripts packaging/build.sh and
# packaging/smoke.sh run by hand. The source tarball is made here from the
# copied tree, where build.sh takes it from `git archive`.
# Each target's build root, pinned by digest so the image a check judged is the
# one it names (constitution S2); TARGET picks one, and Renovate moves the pins.
ARG TARGET
FROM registry.fedoraproject.org/fedora:43@sha256:169d84d08158fc0fec43bc161ee3ca66fefdc88a5840fa615ae759c84c2298aa AS root-fc43
FROM registry.fedoraproject.org/fedora:45@sha256:1bd80cd53c142fe07d25dac925494e02fa7aec33534ea30440e74f85014359a1 AS root-fc45
FROM quay.io/centos/centos:stream10@sha256:b042adb7687b322f80765277b552dcdd9c5f98bd3ca64e2f46175d2ae55fc82b AS root-el10

FROM root-${TARGET} AS build
COPY VERSION pyproject.toml LICENSE README.md /tree/
COPY src/ /tree/src/
COPY skills/ /tree/skills/
COPY packaging/ /packaging/
RUN mkdir /out \
    && version=$(tr -d '[:space:]' < /tree/VERSION) \
    && tar -czf "/out/ashigaru-$version.tar.gz" --transform "s,^tree,ashigaru-$version," -C / tree \
    && ASHIGARU_VERSION=$version bash /packaging/rpmbuild-in-container.sh

# A fresh root, so the RPMs must stand on their own dependencies.
FROM root-${TARGET}
COPY --from=build /out/ /rpms/
COPY packaging/smoke-in-container.sh /packaging/
RUN bash /packaging/smoke-in-container.sh
