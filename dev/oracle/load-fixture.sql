-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Bulk-load the shared Sakila fixture into the schema jOOQ's script created.
-- Each table's file is read through an external table whose columns come from
-- the file's header line; rows are converted to the table's column types on
-- insert. The port's triggers stamp last_update on every insert and Sakila's
-- references form a cycle, so triggers and foreign keys are off while loading.

WHENEVER SQLERROR EXIT FAILURE
SET SERVEROUTPUT ON

DECLARE
    tables   UTL_FILE.FILE_TYPE;
    listing  VARCHAR2(4000);
    name     VARCHAR2(128);
    fixture  UTL_FILE.FILE_TYPE;
    header   VARCHAR2(32767);
    fields   VARCHAR2(32767);
    targets  VARCHAR2(32767);
    selected VARCHAR2(32767);
    kind     VARCHAR2(128);
    item     VARCHAR2(128);
    rest     VARCHAR2(32767);
BEGIN
    FOR t IN (SELECT table_name FROM user_tables) LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE "' || t.table_name || '" DISABLE ALL TRIGGERS';
    END LOOP;
    FOR c IN (SELECT table_name, constraint_name FROM user_constraints WHERE constraint_type = 'R') LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE "' || c.table_name || '" DISABLE CONSTRAINT "' || c.constraint_name || '"';
    END LOOP;
    tables := UTL_FILE.FOPEN('SAKILA_FIXTURE', 'tables.txt', 'r');
    LOOP
        BEGIN
            UTL_FILE.GET_LINE(tables, listing);
        EXCEPTION WHEN NO_DATA_FOUND THEN EXIT;
        END;
        name := UPPER(SUBSTR(listing, 1, INSTR(listing, CHR(9)) - 1));
        fixture := UTL_FILE.FOPEN('SAKILA_FIXTURE', LOWER(name) || '.tsv', 'r', 32767);
        UTL_FILE.GET_LINE(fixture, header);
        UTL_FILE.FCLOSE(fixture);
        fields := NULL; targets := NULL; selected := NULL;
        rest := header || CHR(9);
        WHILE rest IS NOT NULL LOOP
            item := UPPER(SUBSTR(rest, 1, INSTR(rest, CHR(9)) - 1));
            rest := SUBSTR(rest, INSTR(rest, CHR(9)) + 1);
            SELECT data_type INTO kind FROM user_tab_columns
            WHERE table_name = name AND column_name = item;
            fields := fields || CASE WHEN fields IS NOT NULL THEN ', ' END || '"' || item || '" CHAR(32767)';
            targets := targets || CASE WHEN targets IS NOT NULL THEN ', ' END || '"' || item || '"';
            selected := selected || CASE WHEN selected IS NOT NULL THEN ', ' END ||
                CASE
                    WHEN kind = 'DATE' THEN
                        'TO_DATE(SUBSTR(NULLIF("' || item || '", ''\N''), 1, 19), ''YYYY-MM-DD HH24:MI:SS'')'
                    WHEN kind = 'BLOB' THEN
                        'TO_BLOB(HEXTORAW(NULLIF("' || item || '", ''\N'')))'
                    ELSE 'NULLIF("' || item || '", ''\N'')'
                END;
        END LOOP;
        EXECUTE IMMEDIATE
            'CREATE TABLE "EXT_' || name || '" (' || REPLACE(fields, ' CHAR(32767)', ' VARCHAR2(4000)') || ')'
            || ' ORGANIZATION EXTERNAL (TYPE ORACLE_LOADER DEFAULT DIRECTORY sakila_fixture'
            || ' ACCESS PARAMETERS (RECORDS DELIMITED BY NEWLINE CHARACTERSET AL32UTF8 SKIP 1'
            || ' NOBADFILE NOLOGFILE NODISCARDFILE FIELDS TERMINATED BY X''09'''
            || ' MISSING FIELD VALUES ARE NULL (' || fields || '))'
            || ' LOCATION (''' || LOWER(name) || '.tsv'')) REJECT LIMIT 0';
        EXECUTE IMMEDIATE 'INSERT INTO "' || name || '" (' || targets || ') SELECT '
            || selected || ' FROM "EXT_' || name || '"';
        DBMS_OUTPUT.PUT_LINE(name || ': ' || SQL%ROWCOUNT);
        EXECUTE IMMEDIATE 'DROP TABLE "EXT_' || name || '"';
    END LOOP;
    UTL_FILE.FCLOSE(tables);
    COMMIT;
    FOR c IN (SELECT table_name, constraint_name FROM user_constraints WHERE constraint_type = 'R') LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE "' || c.table_name || '" ENABLE CONSTRAINT "' || c.constraint_name || '"';
    END LOOP;
    FOR t IN (SELECT table_name FROM user_tables) LOOP
        EXECUTE IMMEDIATE 'ALTER TABLE "' || t.table_name || '" ENABLE ALL TRIGGERS';
    END LOOP;
END;
/
EXIT
