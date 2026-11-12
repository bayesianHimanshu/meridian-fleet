# Meridian dispatcher image (phase 4b step 2): meridian-dispatcher serve, its locked
# dependencies and the kubectl-ate binary, nothing else. Built by
# `meridian dispatcher build` with a second build context named `ate` that holds the
# kubectl-ate binary built from go.ate.dev/substrate v1.0.0 (its path and sha256 are
# printed by the build and recorded in deploy/dispatcher/dispatcher.yaml).
#
# Same pinned bases and the same locked dependencies as docker/agent.Dockerfile.
ARG PYTHON_IMAGE=python:3.14.10-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv build --wheel --out-dir /dist \
 && uv export --frozen --no-default-groups --no-emit-project --format requirements-txt -o /dist/requirements.txt
RUN uv venv /opt/meridian/venv --python /usr/local/bin/python3.14 \
 && uv pip install --python /opt/meridian/venv/bin/python --compile-bytecode --require-hashes -r /dist/requirements.txt \
 && uv pip install --python /opt/meridian/venv/bin/python --compile-bytecode --no-deps /dist/*.whl

FROM ${PYTHON_IMAGE}
RUN groupadd --gid 1000 dispatcher \
 && useradd --uid 1000 --gid dispatcher --no-create-home --home-dir /tmp --shell /usr/sbin/nologin dispatcher
COPY --from=build /opt/meridian/venv /opt/meridian/venv
COPY --from=ate --chmod=0755 kubectl-ate /usr/local/bin/kubectl-ate
COPY config/default.toml /opt/meridian/config/
ENV PATH=/opt/meridian/venv/bin:$PATH \
    MERIDIAN_CONFIG_FILE=/opt/meridian/config/default.toml \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
USER 1000:1000
WORKDIR /tmp
ENTRYPOINT ["meridian-dispatcher"]
