# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# MySQL 8.4 with the Sakila schema, loaded at start from the shared Sakila
# fixture (dev/sakila, mounted at /fixtures), and the awkward cases the subset
# planner must handle. The schema is jOOQ's, fetched at a pinned commit and
# checked against its SHA-256; see dev/sakila/LICENSE.txt.

FROM mysql:8.4.13@sha256:a81d043930e51745522bd5c430961cf8286f0a23f0a9a2a6e0108f7ea71b0902

ADD --chmod=0644 \
    --checksum=sha256:f2c41c3bf6d6c239941b4f98fb37afad21f6be12b82bf586202529a793ccc2ee \
    https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84/mysql-sakila-db/mysql-sakila-schema.sql \
    /docker-entrypoint-initdb.d/10-sakila-schema.sql
COPY --chmod=0755 load-sakila.sh /docker-entrypoint-initdb.d/20-load-sakila.sh
COPY --chmod=0644 awkward-cases.sql /docker-entrypoint-initdb.d/30-awkward-cases.sql
