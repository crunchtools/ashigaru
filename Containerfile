# The Ashigaru Desk service: quay.io/crunchtools/ashigaru
#
# Built, gated and promoted by ci.yml's image and promote jobs ([tool.sdlc.image]
# in pyproject.toml); not built by hand. The SQLite file lives on the
# /data volume. The root filesystem can run read-only: nothing writes outside
# /data. Settings are environment variables (docs/desk-api.md, Deploy).

# Dependencies come from uv.lock, as the fleet's other Python images take them
# (RT#1564): the service's extra, without the dev group or the project itself.
FROM quay.io/hummingbird/python:3.14-builder AS build
USER 0
RUN pip install --no-cache-dir uv==0.13.7
WORKDIR /src
COPY pyproject.toml uv.lock VERSION README.md LICENSE ./
COPY src/ src/
RUN uv export --frozen --no-dev --no-emit-project --extra desk --extra mcp --format requirements-txt -o /tmp/requirements.txt \
    && python3 -m venv /opt/ashigaru \
    && /opt/ashigaru/bin/pip install --no-cache-dir --no-deps -r /tmp/requirements.txt \
    && /opt/ashigaru/bin/pip install --no-cache-dir --no-deps . \
    && /opt/ashigaru/bin/python -c "import ashigaru.desk.api, ashigaru.desk.mcp.server" \
    && mkdir -p /data && chown 65532:65532 /data

FROM quay.io/hummingbird/python:3.14

LABEL maintainer="fatherlinux <scott.mccarty@crunchtools.com>"
LABEL description="Ashigaru Desk: one queue for every source of change, one owner per item"
LABEL org.opencontainers.image.title="ashigaru"
LABEL org.opencontainers.image.description="The Ashigaru Desk service: REST API over a SQLite queue"
LABEL org.opencontainers.image.source="https://github.com/crunchtools/ashigaru"
LABEL org.opencontainers.image.url="https://github.com/crunchtools/ashigaru"
LABEL org.opencontainers.image.licenses="AGPL-3.0-or-later"
LABEL org.opencontainers.image.vendor="crunchtools"

COPY --from=build /opt/ashigaru /opt/ashigaru
COPY --from=build --chown=65532:65532 /data /data

ENV PATH=/opt/ashigaru/bin:/usr/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASHIGARU_DESK_DB=/data/desk.db \
    ASHIGARU_BIND=0.0.0.0 \
    ASHIGARU_PORT=8080 \
    ASHIGARU_FORGE_AUTHORS_GITHUB=ashigaru-crunchtools[bot]

USER 65532
WORKDIR /data
VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["ashigaru-desk", "health"]

ENTRYPOINT ["ashigaru-desk"]
CMD ["serve"]
