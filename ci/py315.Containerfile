# The Python 3.15 test runtime: the Fedora 45 image the fc45 RPM is built for.
# sdlc ci tests on the gate image's own Python, so this is the one run on the
# newest ([tool.sdlc.ci.py315] in pyproject.toml). The interpreter is the
# distro's, never a download; the dependencies are uv.lock's.
FROM registry.fedoraproject.org/fedora:45
RUN dnf -y -q install python3 python3-pip git \
    && dnf clean all \
    && python3 -m pip install -q --no-cache-dir --root-user-action=ignore uv==0.13.7
