-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Cases jOOQ's Sakila port lacks that the subset planner, the adapter and
-- verification must handle. Sakila already has composite keys (film_actor,
-- film_category), a reference cycle (store.manager_staff_id and
-- staff.store_id), a BLOB (staff.picture), CLOBs, sequences, triggers that
-- stamp last_update, views, object types and a PL/SQL package. All names and
-- contact details are invented. Run as SYSDBA.

WHENEVER SQLERROR EXIT FAILURE
ALTER SESSION SET CONTAINER = FREEPDB1;
ALTER SESSION SET CURRENT_SCHEMA = sakila;

-- A self-reference: who referred whom.
CREATE TABLE customer_referral (
    customer_id INT PRIMARY KEY,
    referred_by INT NULL,
    note VARCHAR2(100),
    CONSTRAINT fk_referral_customer FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_referral_referrer FOREIGN KEY (referred_by) REFERENCES customer_referral (customer_id)
);
INSERT INTO customer_referral (customer_id, referred_by, note)
SELECT customer_id,
       CASE WHEN MOD(customer_id, 5) = 1 THEN NULL ELSE customer_id - 1 END,
       'referral recorded by phone'
FROM customer
WHERE customer_id <= 40;

-- A polymorphic reference with no declared foreign key, holding free text that
-- looks like personal data, with an identity key.
CREATE TABLE customer_note (
    note_id NUMBER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subject_type VARCHAR2(10) NOT NULL CHECK (subject_type IN ('customer', 'film')),
    subject_id INT NOT NULL,
    body CLOB
);
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'customer', customer_id,
       'Asked to be called back on (617) 222-' || LPAD(customer_id, 4, '0')
FROM customer
WHERE customer_id <= 25;
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'film', film_id, 'Requested by a customer'
FROM film
WHERE film_id <= 25;

-- A table with neither a primary key nor foreign keys.
CREATE TABLE audit_event (
    recorded_at TIMESTAMP NOT NULL,
    entity VARCHAR2(20) NOT NULL,
    entity_id INT NOT NULL,
    detail VARCHAR2(100)
);
INSERT INTO audit_event (recorded_at, entity, entity_id, detail)
SELECT TIMESTAMP '2022-02-01 00:00:00' + NUMTODSINTERVAL(rental_id, 'HOUR'), 'rental', rental_id, 'created'
FROM rental
WHERE rental_id <= 100;

-- Types MySQL lacks: a virtual column, time zones, intervals, raw bytes and
-- national characters.
CREATE TABLE inventory_check (
    inventory_id INT PRIMARY KEY,
    checked_at TIMESTAMP(6) WITH TIME ZONE NOT NULL,
    took INTERVAL DAY TO SECOND,
    tag RAW(8),
    note NVARCHAR2(50),
    label VARCHAR2(20) GENERATED ALWAYS AS (CAST('INV-' || TO_CHAR(inventory_id) AS VARCHAR2(20))) VIRTUAL,
    CONSTRAINT fk_check_inventory FOREIGN KEY (inventory_id) REFERENCES inventory (inventory_id)
);
INSERT INTO inventory_check (inventory_id, checked_at, took, tag, note)
SELECT inventory_id,
       FROM_TZ(TIMESTAMP '2022-03-01 09:00:00', '-05:00') + NUMTODSINTERVAL(MOD(inventory_id, 30), 'DAY'),
       NUMTODSINTERVAL(inventory_id, 'MINUTE'),
       HEXTORAW(LPAD(TO_CHAR(inventory_id, 'FMXXXX'), 8, '0')),
       -- Escaped, so the text does not depend on SQL*Plus's client character set.
       CASE WHEN MOD(inventory_id, 2) = 0 THEN UNISTR('R\00E9gion nord') ELSE UNISTR('\6771\4EAC') END
FROM inventory
WHERE inventory_id <= 50;

-- A table a spec is expected to exclude, and a view that depends on it.
CREATE TABLE dba_maint_runs (
    run_id INT PRIMARY KEY,
    ran_at DATE NOT NULL
);
INSERT INTO dba_maint_runs VALUES (1, DATE '2022-03-01');
CREATE VIEW dba_recent_runs AS SELECT run_id FROM dba_maint_runs;

COMMIT;
EXIT
