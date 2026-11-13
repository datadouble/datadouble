#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Start SQL Server and, the first time, create Sakila: jOOQ's schema, the rows
# bulk-loaded from the shared fixture, then the awkward cases. The image has no
# initialization hook of its own, so this script is the entry point.
set -euo pipefail

/opt/mssql/bin/sqlservr &
server=$!
trap 'kill -TERM "$server"; wait "$server"' TERM INT

client() {
    /opt/mssql-tools18/bin/sqlcmd -C -b -r 1 -I -S localhost -U sa -P "$MSSQL_SA_PASSWORD" "$@"
}
until client -Q "SELECT 1" > /dev/null 2>&1; do
    sleep 1
done

if [ "$(client -h -1 -W -Q "SET NOCOUNT ON; SELECT DB_ID('sakila')")" = "NULL" ]; then
    client -i /opt/datadouble/10-sakila-schema.sql > /dev/null
    # BULK INSERT reads an empty field as NULL (KEEPNULLS); the fixture
    # writes NULL as \N and has no other escapes and no empty strings. It is
    # ASCII, so no code page is needed (SQL Server on Linux accepts none).
    rows=$(mktemp -d)
    while IFS="$(printf '\t')" read -r table _rows; do
        sed 's/\\N//g' "/fixtures/sakila/$table.tsv" > "$rows/$table.tsv"
        client -d sakila -Q "BULK INSERT dbo.$table FROM '$rows/$table.tsv'
            WITH (FIELDTERMINATOR = '\t', ROWTERMINATOR = '0x0a', FIRSTROW = 2,
                  KEEPIDENTITY, KEEPNULLS, TABLOCK)" < /dev/null > /dev/null
    done < /fixtures/sakila/tables.txt
    rm -rf "$rows"
    client -i /opt/datadouble/30-awkward-cases.sql > /dev/null
fi

wait "$server"
