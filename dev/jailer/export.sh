#!/bin/sh
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Build Jailer's data model of the Compose Sakila, then export sakila.jm's
# rows as JSON, unsorted (Sakila's reference cycles have no insert order), into
# /jailer-out for the comparison test. Jailer collects keys in an embedded
# database, so nothing is written to the source.
set -eu
cd /opt/jailer
driver=com.mysql.cj.jdbc.Driver
url=jdbc:mysql://mysql:3306/sakila
jar=lib/mysql-connector-java-8.0.21.jar
out=/jailer-out
mkdir -p "$out"
sh jailer.sh build-model -schema sakila "$driver" "$url" datadouble datadouble -jdbcjar "$jar" \
    -datamodel "$out/datamodel"
sh jailer.sh export sakila.jm "$driver" "$url" datadouble datadouble -jdbcjar "$jar" \
    -datamodel "$out/datamodel" -e "$out/export.json" -format JSON -no-sorting \
    -scope LOCAL_DATABASE
cp sakila.jm "$out/sakila.jm"
