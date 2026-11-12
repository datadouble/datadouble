# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Stages:
#   builder    Datadouble and every first-party adapter, for the runtime image
#   tools      the programs Datadouble drives: the docker command and kubectl
#              (container runtimes and Kubernetes adapters) and cosign (signing)
#   test       the workspace with its development tools on top of tools; runs
#              the test suite as an unprivileged user (docker compose)
#   runtime    the published image: Datadouble, its adapters and those tools
#
# Engine software (servers, dump and restore tools) is not installed anywhere:
# it runs in the engine vendor's own images, through a container runtime.
# Base images are pinned by digest; Dependabot keeps them current.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.15-slim-trixie@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS base
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_FROZEN=1
WORKDIR /src

FROM base AS builder
ENV UV_PROJECT_ENVIRONMENT=/opt/datadouble
COPY pyproject.toml uv.lock ./
COPY core/ core/
COPY adapters/ adapters/
COPY conformance/ conformance/
RUN uv sync --no-dev --no-editable --package datadouble \
        --package datadouble-btrfs --package datadouble-docker --package datadouble-kubernetes \
        --package datadouble-mongodb --package datadouble-mysql --package datadouble-postgres --package datadouble-s3 \
        --package datadouble-vault

FROM base AS tools
# The docker command (static), kubectl and cosign, all pinned.
COPY --from=docker:29-cli@sha256:2357639ad1ea7d0194134bf3292622fd4e562483966ed674e5d7c945b5f3b28d \
    /usr/local/bin/docker /usr/local/bin/docker
ADD --chmod=0755 \
    --checksum=sha256:c04487523e7aa7d1493f7808174c899db93d7a9cf57531420cd76b9bd415f8f4 \
    https://dl.k8s.io/release/v1.37.2/bin/linux/amd64/kubectl \
    /usr/local/bin/kubectl
ADD --chmod=0755 \
    --checksum=sha256:424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa \
    https://github.com/sigstore/cosign/releases/download/v3.1.4/cosign-linux-amd64 \
    /usr/local/bin/cosign

FROM tools AS test
# git for the source checker's tests, btrfs-progs for the btrfs replicate adapter.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends btrfs-progs git \
    && rm -rf /var/lib/apt/lists/*
# The suite runs as an ordinary user, as Datadouble should. /work is where the
# Compose file mounts the volume shared with the Docker-in-Docker daemon.
RUN useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin tester \
    && install --directory --owner tester --group tester /src /reports /work
USER tester
COPY --chown=tester:tester pyproject.toml uv.lock ./
COPY --chown=tester:tester core/pyproject.toml core/README.md core/LICENSE core/
COPY --chown=tester:tester core/src/datadouble/__init__.py core/src/datadouble/
COPY --chown=tester:tester adapters/btrfs/pyproject.toml adapters/btrfs/README.md adapters/btrfs/LICENSE adapters/btrfs/
COPY --chown=tester:tester adapters/docker/pyproject.toml adapters/docker/README.md adapters/docker/LICENSE adapters/docker/
COPY --chown=tester:tester adapters/mongodb/pyproject.toml adapters/mongodb/README.md adapters/mongodb/LICENSE adapters/mongodb/
COPY --chown=tester:tester adapters/kubernetes/pyproject.toml adapters/kubernetes/README.md adapters/kubernetes/LICENSE adapters/kubernetes/
COPY --chown=tester:tester adapters/mysql/pyproject.toml adapters/mysql/README.md adapters/mysql/LICENSE adapters/mysql/
COPY --chown=tester:tester adapters/postgres/pyproject.toml adapters/postgres/README.md adapters/postgres/LICENSE adapters/postgres/
COPY --chown=tester:tester adapters/s3/pyproject.toml adapters/s3/README.md adapters/s3/LICENSE adapters/s3/
COPY --chown=tester:tester adapters/vault/pyproject.toml adapters/vault/README.md adapters/vault/LICENSE adapters/vault/
COPY --chown=tester:tester conformance/pyproject.toml conformance/README.md conformance/LICENSE conformance/
RUN uv sync --no-install-workspace
COPY --chown=tester:tester . .
RUN uv sync
CMD ["uv", "run", "pytest", "--cov", "--cov-report=term-missing", "--cov-report=xml:/reports/coverage.xml"]

FROM tools AS runtime
LABEL org.opencontainers.image.source=https://github.com/datadouble/datadouble \
      org.opencontainers.image.description="Realistic, safe copies of production data for development and testing" \
      org.opencontainers.image.licenses=MIT
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin datadouble
COPY --from=builder /opt/datadouble /opt/datadouble
ENV PATH=/opt/datadouble/bin:$PATH
USER 10001:10001
ENTRYPOINT ["datadouble"]
CMD ["--help"]
