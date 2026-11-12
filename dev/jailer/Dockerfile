# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Jailer (Apache-2.0), for the conformance test that compares its key sets with
# Datadouble's on Sakila. The release is pinned by version and SHA-256; it
# bundles the MySQL JDBC driver.
FROM eclipse-temurin:21-jre@sha256:18bb4a91a3dea3f03ee31d3755af34dfb40f904fc77ab56cdc2f092e32fff9c2

ADD --checksum=sha256:0cd07186ba9464f37d384c0fd8a68b00d1e4b2eb3900c98e540bc692901317b4 \
    https://github.com/Wisser/Jailer/releases/download/v17.3.1/jailer_17.3.1.zip /tmp/jailer.zip
RUN apt-get update && apt-get install --yes --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/* \
    && unzip -q /tmp/jailer.zip -d /opt && rm /tmp/jailer.zip
COPY --chmod=0644 sakila.jm /opt/jailer/sakila.jm
COPY --chmod=0755 export.sh /usr/local/bin/export-sakila
CMD ["export-sakila"]
