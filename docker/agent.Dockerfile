# Meridian agent image: meridian-worker and its locked dependencies, nothing else.
# Built by `meridian image build`; tagged meridian-agent:<package version>.
#
# Bytecode is compiled at build time: the sandbox cannot write it, and compiling numpy,
# scipy and polars on every run cost about 1.6 s (worker exec 3.0 s, now 1.4 s).
#
# Base: python:3.14.10-slim, pinned by digest. Digest read with
#   docker image inspect python:3.14.10-slim --format '{{json .RepoDigests}}'
# after `docker pull python:3.14.10-slim` on 2026-11-07 (linux/amd64 host).
# uv: ghcr.io/astral-sh/uv:0.13.7 (the version that wrote uv.lock), pinned the same way.
ARG PYTHON_IMAGE=python:3.14.10-slim@sha256:af8a5aa14780025dbfbcfbc81bd8a91ebd87711dd5b0f48ed71090949dd7cb1d
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# The wheel, and the runtime dependencies exactly as locked (no dependency groups: not
# dev, not api; with hashes).
RUN uv build --wheel --out-dir /dist \
 && uv export --frozen --no-default-groups --no-emit-project --format requirements-txt -o /dist/requirements.txt
RUN uv venv /opt/meridian/venv --python /usr/local/bin/python3.14 \
 && uv pip install --python /opt/meridian/venv/bin/python --compile-bytecode --require-hashes -r /dist/requirements.txt \
 && uv pip install --python /opt/meridian/venv/bin/python --compile-bytecode --no-deps /dist/*.whl

# Final stage: the same base, the venv and the config. No uv, no compilers, no sources.
FROM ${PYTHON_IMAGE}
RUN groupadd --gid 1000 sandbox \
 && useradd --uid 1000 --gid sandbox --no-create-home --home-dir /tmp --shell /usr/sbin/nologin sandbox
# One folder per staged area (meridian.sandbox.policies.STAGE_AREAS). Landlock rules name
# paths that exist when the sandbox starts, so the folders are made here; a policy lists
# only the areas its zone may use. /stage itself stays root's and is in no policy.
RUN mkdir -m 0755 /stage \
 && for area in meta blinded ops unblinded narr lm tmf; do \
      install -d -m 0700 -o sandbox -g sandbox "/stage/$area"; \
    done
COPY --from=build /opt/meridian/venv /opt/meridian/venv
COPY config/default.toml config/sites.toml config/safety_terms.toml config/tmf_expected.toml /opt/meridian/config/
ENV PATH=/opt/meridian/venv/bin:$PATH \
    MERIDIAN_CONFIG_FILE=/opt/meridian/config/default.toml \
    PYTHONDONTWRITEBYTECODE=1
USER sandbox
WORKDIR /tmp
