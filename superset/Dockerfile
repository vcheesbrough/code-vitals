# syntax=docker/dockerfile:1
# code-vitals-superset: Apache Superset 6.1.0 plus what it lacks for
# code-vitals — the Postgres driver and authlib (for Authentik OIDC) — and our
# configuration. Built and published by the push pipeline beside the collector,
# under the same version; deployments use it by digest and never rebuild it.
FROM apache/superset:6.1.0@sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88

USER root
RUN uv pip install --python /app/.venv/bin/python --no-cache \
    psycopg2-binary==2.9.13 \
    authlib==1.9.1
COPY superset/superset_config.py superset/code_vitals_auth.py /app/pythonpath/
COPY --chmod=0755 superset/bootstrap.sh /app/docker/code-vitals-bootstrap.sh

ARG RELEASE_TAG=0.0.0-local
ARG GIT_REVISION=unknown
LABEL org.opencontainers.image.title="code-vitals-superset" \
      org.opencontainers.image.description="Apache Superset configured for code-vitals (Authentik sign-in, read-only datasource)" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0" \
      org.opencontainers.image.url="https://github.com/vcheesbrough/code-vitals" \
      org.opencontainers.image.source="https://github.com/vcheesbrough/code-vitals" \
      org.opencontainers.image.documentation="https://github.com/vcheesbrough/code-vitals/blob/main/README.md" \
      org.opencontainers.image.authors="Vincent Cheesbrough" \
      org.opencontainers.image.vendor="Vincent Cheesbrough" \
      org.opencontainers.image.base.name="apache/superset:6.1.0" \
      org.opencontainers.image.base.digest="sha256:16b50bbef6648912a79e3293d418fefa743ce555d36c5af6b85d859119ed7f88"
USER superset
