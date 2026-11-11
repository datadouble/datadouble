# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Stages:
#   test     the workspace with its development tools; runs the test suite
#            (docker compose uses this)
#   runtime  the published image: the core package and nothing else
#
# Base images are pinned by digest; Dependabot keeps them current.

FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base
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
RUN uv sync --no-dev --package datadouble --no-editable

FROM base AS test
# git: the source checker's tests build throwaway repositories.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/README.md core/LICENSE core/
COPY core/src/datadouble/__init__.py core/src/datadouble/
RUN uv sync --no-install-workspace
COPY . .
RUN uv sync
CMD ["uv", "run", "pytest", "--cov", "--cov-report=term-missing", "--cov-report=xml:/reports/coverage.xml"]

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runtime
LABEL org.opencontainers.image.source=https://github.com/datadouble/datadouble \
      org.opencontainers.image.description="Realistic, safe copies of production data for development and testing" \
      org.opencontainers.image.licenses=MIT
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin datadouble
COPY --from=builder /opt/datadouble /opt/datadouble
ENV PATH=/opt/datadouble/bin:$PATH
USER 10001:10001
ENTRYPOINT ["datadouble"]
CMD ["--help"]
