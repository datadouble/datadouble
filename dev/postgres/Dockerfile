# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# PostgreSQL 18.4 with the Pagila sample database and the awkward cases the
# subset planner must handle. Pagila is fetched at a pinned commit and checked
# against its SHA-256; see PAGILA-LICENSE.txt for its licence.

# Pagila's film_embedding table uses pgvector. The pgvector project's image is
# the official PostgreSQL image with the extension added, so the source is
# exactly the server the conformance spec's working image runs; installing the
# extension from apt would upgrade PostgreSQL past it.
FROM pgvector/pgvector:0.8.2-pg18-trixie@sha256:b7337db8fe39d12fe8ecb0003c72680f24479813a744b43154eee6f2eab5a5f3

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
