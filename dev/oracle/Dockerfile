# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Oracle Database Free 23.26 with the Sakila schema in FREEPDB1, loaded at
# first start from the shared Sakila fixture (dev/sakila, mounted at
# /fixtures), and the awkward cases the subset planner and the adapter must
# handle. The schema is jOOQ's port, fetched at a pinned commit and checked
# against its SHA-256; see dev/sakila/LICENSE.txt.

FROM container-registry.oracle.com/database/free:23.26.3.0-lite@sha256:cf540c3fa190d7cffad08c491652ac07fc70314e510f6b87890449517d565e94

ADD --chown=oracle:oinstall --chmod=0644 \
    --checksum=sha256:6d8943ba87103fcf50356e63727fd2b5be009b2061cfcec1a22db4054d809aa0 \
    https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84/oracle-sakila-db/oracle-sakila-schema.sql \
    /opt/oracle/sakila/10-sakila-schema.sql
ADD --chown=oracle:oinstall --chmod=0644 \
    --checksum=sha256:f77ccbb8af54b2e452757815b2f15aac25d09a1b2f53de2a36386dff75b34e44 \
    https://raw.githubusercontent.com/jOOQ/sakila/e089a5b1ec9af0df7a9c6a5d47d49fa1736a4e84/oracle-sakila-db/oracle-sakila-schema-pl-sql.sql \
    /opt/oracle/sakila/15-sakila-pl-sql.sql
COPY --chown=oracle:oinstall --chmod=0644 load-fixture.sql awkward-cases.sql /opt/oracle/sakila/
# ADD and COPY give the directory they create the files' mode.
USER root
RUN chmod 0755 /opt/oracle/sakila
USER oracle
# Startup scripts run on every start; this one does its work only once.
COPY --chown=oracle:oinstall --chmod=0755 sakila.sh /opt/oracle/scripts/startup/sakila.sh
