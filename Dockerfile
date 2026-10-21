# syntax=docker/dockerfile:1
# The code-vitals collector image: Python + lizard (pinned in uv.lock), tokei
# (pinned in tool-versions.env, passed as TOKEI_VERSION) and git.
#
# Stages: tokei (built from source — its releases ship no Linux binaries
# since v13) → build (the app's virtualenv) → test (lint, types, tests, script
# tests; the runtime stage copies its marker, so a red test fails the build)
# → runtime.

ARG PYTHON_IMAGE=python:3.13.16-slim@sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f

FROM rust:1.98.1@sha256:462a9af3c54fb4718850d3c602fc0e54452c20b1c12a4e4080fdb001d4b9acbf AS tokei
ARG TOKEI_VERSION
RUN test -n "$TOKEI_VERSION" || { echo "TOKEI_VERSION build arg is required" >&2; exit 1; }
RUN --mount=type=cache,id=code-vitals-cargo-registry,target=/usr/local/cargo/registry \
    cargo install --locked --root /opt/tokei tokei --version "$TOKEI_VERSION"

FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,id=code-vitals-uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN --mount=type=cache,id=code-vitals-uv-cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM build AS test
RUN --mount=type=cache,id=code-vitals-uv-cache,target=/root/.cache/uv \
    uv sync --frozen
COPY tests ./tests
COPY scripts ./scripts
COPY tool-versions.env ./
RUN uv run --frozen ruff format --check \
    && uv run --frozen ruff check \
    && uv run --frozen mypy \
    && uv run --frozen pytest -q
RUN for t in scripts/test-*.sh; do sh "$t" || exit 1; done \
    && touch /tests-passed

FROM ${PYTHON_IMAGE} AS runtime
ARG RELEASE_TAG=0.0.0-local
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.title="code-vitals-collector" \
      org.opencontainers.image.description="code-vitals collector — records measures of a project's code at every trunk commit" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.url="https://github.com/vcheesbrough/code-vitals" \
      org.opencontainers.image.source="https://github.com/vcheesbrough/code-vitals" \
      org.opencontainers.image.documentation="https://github.com/vcheesbrough/code-vitals/blob/main/README.md" \
      org.opencontainers.image.authors="Vincent Cheesbrough" \
      org.opencontainers.image.vendor="Vincent Cheesbrough" \
      org.opencontainers.image.base.name="python:3.13.16-slim" \
      org.opencontainers.image.base.digest="sha256:cf50a4f6ac4f026458b08d5222023d700ac806410dff5e9ca8728fa44f2b850f"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    # Measured repos are cloned by CI as another user; without this git refuses
    # them as "dubious ownership".
    && git config --system --add safe.directory '*'
COPY --from=tokei /opt/tokei/bin/tokei /usr/local/bin/tokei
COPY --from=build /app/.venv /app/.venv
COPY --from=test /tests-passed /etc/code-vitals/tests-passed
# The build's identity, as a file the deployment cannot override
# (src/code_vitals/build_info.py).
RUN printf '{"version": "%s", "revision": "%s"}\n' "$RELEASE_TAG" "$GIT_REVISION" \
    > /etc/code-vitals/build-info.json
RUN useradd --system --uid 10001 --create-home --home-dir /home/code-vitals code-vitals
USER code-vitals
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
ENTRYPOINT ["code-vitals"]
