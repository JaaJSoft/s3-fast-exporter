FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS build
COPY --from=ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra uvloop
COPY README.md LICENSE ./
COPY exporter ./exporter
RUN uv sync --frozen --no-dev --no-editable --extra uvloop

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
LABEL org.opencontainers.image.title="s3-fast-exporter" \
      org.opencontainers.image.description="Prometheus exporter for S3 buckets and objects" \
      org.opencontainers.image.source="https://github.com/JaaJSoft/s3-fast-exporter" \
      org.opencontainers.image.vendor="JaaJSoft" \
      org.opencontainers.image.licenses="Apache-2.0"
# pip only installs packages, and it vendors its own copies of urllib3, msgpack and setuptools
# that lag behind their fixes.
RUN python -m pip uninstall --yes --no-cache-dir pip
COPY --from=build /opt/venv /opt/venv
COPY LICENSE NOTICE /usr/share/doc/s3-fast-exporter/

USER 65534:65534
EXPOSE 9340
ENTRYPOINT ["/opt/venv/bin/s3-fast-exporter"]
