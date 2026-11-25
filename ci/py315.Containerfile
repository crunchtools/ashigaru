# The Python 3.15 test runtime: the Fedora 45 image the fc45 RPM is built for.
# sdlc ci tests on the gate image's own Python, so this is the one run on the
# newest ([tool.sdlc.ci.py315] in pyproject.toml). The interpreter is the
# distro's, never a download; the dependencies are uv.lock's.
FROM registry.fedoraproject.org/fedora:45@sha256:db14be5698ea04f1f9a4f4fb31945205a696a01f1466cf4a7e5f5a77ca3c7a82
RUN dnf -y -q install python3 python3-pip git \
    && dnf clean all \
    && python3 -m pip install -q --no-cache-dir --root-user-action=ignore uv==0.13.7
# The run is only worth its name on 3.15: a Fedora whose python3 moved on fails
# the build here rather than passing the check on another version.
RUN python3 -c 'import sys; assert sys.version_info[:2] == (3, 15), sys.version'
