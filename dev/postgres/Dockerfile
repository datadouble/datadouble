# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# PostgreSQL 18 with the Pagila sample database and the awkward cases the
# subset planner must handle. Pagila is fetched at a pinned commit and checked
# against its SHA-256; see PAGILA-LICENSE.txt for its licence.

FROM postgres:18-trixie@sha256:e3d24f6906d0760bf4da0dae872415391ededefad23fd471c47dc53b0ddf3c42

# Pagila's film_embedding table uses pgvector, from the PGDG repository the
# official image already configures.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends postgresql-18-pgvector \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 roles.sql /docker-entrypoint-initdb.d/05-roles.sql
ADD --chmod=0644 \
    --checksum=sha256:071ee940a73c8f4fad2997185788065291607e2c55559e3747959e8275391536 \
    https://raw.githubusercontent.com/devrimgunduz/pagila/6ea53f812254c91a602f61c0ef62beca3fa676b1/pagila-schema.sql \
    /docker-entrypoint-initdb.d/10-pagila-schema.sql
ADD --chmod=0644 \
    --checksum=sha256:a88efa94c7ae8bc9cf55def4efc9f164d064d5b9cd93f11719ba3b5ace1602f7 \
    https://raw.githubusercontent.com/devrimgunduz/pagila/6ea53f812254c91a602f61c0ef62beca3fa676b1/pagila-data.sql \
    /docker-entrypoint-initdb.d/20-pagila-data.sql
COPY --chmod=0644 awkward-cases.sql /docker-entrypoint-initdb.d/30-awkward-cases.sql
