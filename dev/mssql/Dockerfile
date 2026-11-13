# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# SQL Server 2022 (Developer edition) with the Sakila schema, loaded at start
# from the shared Sakila fixture (dev/sakila, mounted at /fixtures), and the
# awkward cases the subset planner and the adapter must handle. The schema is
# jOOQ's port, fetched at a pinned commit and checked against its SHA-256; see
# dev/sakila/LICENSE.txt.

FROM mcr.microsoft.com/mssql/server:2022-CU28-ubuntu-22.04@sha256:4525e3efac9f3e241cb003bb2ff2a82818b502f37e12049d788b1a9fa7e13b7f

ADD --chmod=0644 \
    --checksum=sha256:994d6f2c71aa7c25a7d661ba59c1d8125fac7fe112724231bfebd1fa4ea7e03b \
    https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84/sql-server-sakila-db/sql-server-sakila-schema.sql \
    /opt/datadouble/10-sakila-schema.sql
COPY --chmod=0644 awkward-cases.sql /opt/datadouble/30-awkward-cases.sql
COPY --chmod=0755 start.sh /opt/datadouble/start.sh
# ADD and COPY give the directory they create the files' mode.
USER root
RUN chmod 0755 /opt/datadouble
USER mssql

ENTRYPOINT ["/opt/datadouble/start.sh"]
