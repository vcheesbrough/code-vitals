# syntax=docker/dockerfile:1
# code-vitals-plugin: the Woodpecker plugin a repo adds to be measured (README
# § Measuring a repo). The collector image of the same build plus the docker
# CLI: `code-vitals plugin` validates the step's settings with the collector's
# own validator, then starts that build's collector on code-vitals' prod
# network through the mounted docker socket (src/code_vitals/plugin.py).
# Built, checked and published by the push pipeline beside the collector, under
# the same version; only a prod deployment moves its :stable.
ARG COLLECTOR_IMAGE=code-vitals-collector:local
FROM docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS docker-cli

FROM ${COLLECTOR_IMAGE}
# The CLI only (a static binary); the daemon is the host's, through the socket.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
# The collector it is built on (its ref and image id), recorded as its base.
ARG COLLECTOR_IMAGE
ARG COLLECTOR_ID=unknown
LABEL org.opencontainers.image.title="code-vitals-plugin" \
      org.opencontainers.image.description="code-vitals Woodpecker plugin — runs the collector for the repo whose pipeline it is in" \
      org.opencontainers.image.base.name="${COLLECTOR_IMAGE}" \
      org.opencontainers.image.base.digest="${COLLECTOR_ID}"
# The socket is root's (or the docker group's) on the host; the collector it
# starts runs as its own unprivileged user.
USER root
ENTRYPOINT ["code-vitals", "plugin"]
