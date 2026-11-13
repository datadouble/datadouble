#!/bin/bash
# Copyright (c) 2026- Tight Line LLC
# SPDX-License-Identifier: MIT

# Create Sakila in FREEPDB1 the first time the database starts: the user,
# jOOQ's schema and PL/SQL, the rows from the shared fixture, then the
# awkward cases. The image runs this from its startup scripts on every start,
# so it checks first; the image sources it, so it must not exit the shell.
sakila() {
    local done
    done=$(sqlplus -s / as sysdba <<'EOF'
SET HEADING OFF FEEDBACK OFF
ALTER SESSION SET CONTAINER = FREEPDB1;
SELECT COUNT(*) FROM dba_users WHERE username = 'SAKILA';
EOF
)
    if [ "$(echo "$done" | tr -d '[:space:]')" != "0" ]; then
        return 0
    fi
    sqlplus -s / as sysdba <<'EOF'
WHENEVER SQLERROR EXIT FAILURE
ALTER SESSION SET CONTAINER = FREEPDB1;
-- The lite image's pluggable database has no USERS tablespace.
CREATE TABLESPACE sakila_data
    DATAFILE '/opt/oracle/oradata/FREE/FREEPDB1/sakila01.dbf' SIZE 64M AUTOEXTEND ON;
CREATE USER sakila IDENTIFIED BY "Datadouble-1"
    DEFAULT TABLESPACE sakila_data QUOTA UNLIMITED ON sakila_data;
GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW, CREATE SEQUENCE, CREATE TRIGGER,
      CREATE PROCEDURE, CREATE TYPE, CREATE SYNONYM TO sakila;
CREATE USER datadouble IDENTIFIED BY "Datadouble-1";
GRANT DBA TO datadouble;
CREATE DIRECTORY sakila_fixture AS '/fixtures/sakila';
GRANT READ ON DIRECTORY sakila_fixture TO sakila;
EOF
    # jOOQ's scripts end some statements with both ";" and "/", which runs
    # them twice, so they run without stopping on errors and only errors
    # other than those repeats (an object or constraint that exists) are shown.
    sqlplus -s sakila/Datadouble-1@localhost:1521/FREEPDB1 \
        @/opt/oracle/sakila/10-sakila-schema.sql < /dev/null | grep -E "^(ORA|SP2|PLS)-" | grep -vE "^ORA-(00955|02275|02264|02260):" || true
    sqlplus -s sakila/Datadouble-1@localhost:1521/FREEPDB1 \
        @/opt/oracle/sakila/15-sakila-pl-sql.sql < /dev/null | grep -E "^(ORA|SP2|PLS)-" | grep -vE "^ORA-(00955|02275|02264|02260):" || true
    sqlplus -s sakila/Datadouble-1@localhost:1521/FREEPDB1 \
        @/opt/oracle/sakila/load-fixture.sql < /dev/null
    sqlplus -s / as sysdba @/opt/oracle/sakila/awkward-cases.sql < /dev/null
}
sakila
