# Build the ashigaru RPMs on a distro and smoke-test them in a fresh one: the
# pull-request half of .github/workflows/rpm.yml, run by sdlc ci
# ([tool.sdlc.ci.rpm-*] in pyproject.toml). Nothing is published from here.
#   podman build -f packaging/rpm.Containerfile --build-arg BUILD_ROOT=<image> .
# The build is packaging/rpmbuild-in-container.sh and the smoke test
# packaging/smoke-in-container.sh, the same scripts packaging/build.sh and
# packaging/smoke.sh run by hand. The source tarball is made here from the
# copied tree, where build.sh takes it from `git archive`.
ARG BUILD_ROOT

FROM ${BUILD_ROOT} AS build
COPY VERSION pyproject.toml LICENSE README.md /tree/
COPY src/ /tree/src/
COPY skills/ /tree/skills/
COPY packaging/ /packaging/
RUN mkdir /out \
    && version=$(tr -d '[:space:]' < /tree/VERSION) \
    && tar -czf "/out/ashigaru-$version.tar.gz" --transform "s,^tree,ashigaru-$version," -C / tree \
    && ASHIGARU_VERSION=$version bash /packaging/rpmbuild-in-container.sh

# A fresh root, so the RPMs must stand on their own dependencies.
FROM ${BUILD_ROOT}
COPY --from=build /out/ /rpms/
COPY packaging/smoke-in-container.sh /packaging/
RUN bash /packaging/smoke-in-container.sh
