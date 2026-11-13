-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Cases jOOQ's Sakila port lacks that the subset planner, the adapter and
-- verification must handle. Sakila already has composite keys (film_actor,
-- film_category), a reference cycle (store.manager_staff_id and
-- staff.store_id), an IMAGE column (staff.picture), identity columns, check
-- constraints and views. All names and contact details are invented.

USE sakila;
GO

-- The source must allow snapshot isolation, which the adapter reads under.
ALTER DATABASE sakila SET ALLOW_SNAPSHOT_ISOLATION ON;
GO

-- MySQL's Sakila keeps film_text in step with film through triggers; the port
-- leaves them out. Fill film_text once, then add the triggers, which loading
-- and anonymization must not fire.
INSERT INTO film_text (film_id, title, description)
SELECT film_id, title, description FROM film;
GO
CREATE TRIGGER film_text_sync ON film AFTER INSERT, UPDATE AS
BEGIN
    SET NOCOUNT ON;
    DELETE ft FROM film_text ft JOIN inserted i ON ft.film_id = i.film_id;
    -- inserted cannot hold film.description, a TEXT column.
    INSERT INTO film_text (film_id, title, description)
    SELECT f.film_id, f.title, f.description FROM film f JOIN inserted i ON f.film_id = i.film_id;
END;
GO

-- A self-reference: who referred whom.
CREATE TABLE customer_referral (
    customer_id INT NOT NULL PRIMARY KEY,
    referred_by INT NULL,
    note VARCHAR(100),
    CONSTRAINT fk_referral_customer FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_referral_referrer FOREIGN KEY (referred_by) REFERENCES customer_referral (customer_id)
);
INSERT INTO customer_referral (customer_id, referred_by, note)
SELECT customer_id,
       CASE WHEN customer_id % 5 = 1 THEN NULL ELSE customer_id - 1 END,
       'referral recorded by phone'
FROM customer
WHERE customer_id <= 40
ORDER BY customer_id;
GO

-- A polymorphic reference with no declared foreign key, holding free text that
-- looks like personal data.
CREATE TABLE customer_note (
    note_id INT IDENTITY PRIMARY KEY,
    subject_type VARCHAR(10) NOT NULL CHECK (subject_type IN ('customer', 'film')),
    subject_id INT NOT NULL,
    body NVARCHAR(MAX)
);
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'customer', customer_id,
       CONCAT('Asked to be called back on (617) 222-', RIGHT(CONCAT('0000', customer_id), 4))
FROM customer
WHERE customer_id <= 25;
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'film', film_id, N'Requested by a customer'
FROM film
WHERE film_id <= 25;
GO

-- A table with neither a primary key nor foreign keys.
CREATE TABLE audit_event (
    recorded_at DATETIME2 NOT NULL,
    entity VARCHAR(20) NOT NULL,
    entity_id INT NOT NULL,
    detail VARCHAR(100)
);
INSERT INTO audit_event (recorded_at, entity, entity_id, detail)
SELECT DATEADD(HOUR, rental_id, '2022-02-01T00:00:00'), 'rental', rental_id, 'created'
FROM rental
WHERE rental_id <= 100;
GO

-- A geometry column, which travels as SQL Server's binary form.
CREATE TABLE store_location (
    store_id INT NOT NULL PRIMARY KEY,
    location GEOMETRY NOT NULL,
    CONSTRAINT fk_location_store FOREIGN KEY (store_id) REFERENCES store (store_id)
);
INSERT INTO store_location (store_id, location)
SELECT store_id, geometry::Point(store_id, store_id * 2, 0)
FROM store;
GO

-- Columns loading cannot write: a computed column and a rowversion.
CREATE TABLE inventory_check (
    inventory_id INT NOT NULL PRIMARY KEY,
    checked_at DATETIME2 NOT NULL,
    label AS ('INV-' + CAST(inventory_id AS VARCHAR(10))) PERSISTED,
    version ROWVERSION,
    CONSTRAINT fk_check_inventory FOREIGN KEY (inventory_id) REFERENCES inventory (inventory_id)
);
INSERT INTO inventory_check (inventory_id, checked_at)
SELECT inventory_id, DATEADD(DAY, inventory_id % 30, '2022-03-01T09:00:00')
FROM inventory
WHERE inventory_id <= 50;
GO

-- A second schema, with a reference into dbo and types MySQL lacks.
CREATE SCHEMA sales;
GO
CREATE TABLE sales.region_target (
    target_id UNIQUEIDENTIFIER NOT NULL PRIMARY KEY,
    store_id INT NOT NULL,
    period_start DATETIMEOFFSET NOT NULL,
    target DECIMAL(10, 2) NOT NULL,
    note NVARCHAR(100),
    CONSTRAINT fk_target_store FOREIGN KEY (store_id) REFERENCES dbo.store (store_id)
);
INSERT INTO sales.region_target (target_id, store_id, period_start, target, note)
VALUES
    ('6f1c1b0e-2f43-4a51-9a4e-1d2c3b4a5f60', 1, '2022-01-01T00:00:00+00:00', 12000.00, N'Région nord – objectif'),
    ('0b7e3f2a-8c1d-4e6f-b2a9-3c4d5e6f7a81', 2, '2022-01-01T00:00:00-05:00', 9500.50, N'東京支店');
GO

-- A table a spec is expected to exclude, and a view that depends on it.
CREATE TABLE dba_maint_runs (
    run_id INT IDENTITY PRIMARY KEY,
    ran_at DATETIME2 NOT NULL
);
INSERT INTO dba_maint_runs (ran_at) VALUES ('2022-03-01T00:00:00');
GO
CREATE VIEW dba_recent_runs AS SELECT run_id FROM dba_maint_runs;
GO

-- The test login reads Sakila and makes scratch databases of its own.
USE master;
CREATE LOGIN datadouble WITH PASSWORD = 'Datadouble-1', CHECK_POLICY = OFF;
ALTER SERVER ROLE sysadmin ADD MEMBER datadouble;
GO
