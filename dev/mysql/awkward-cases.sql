-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Cases Sakila lacks that the subset planner and verification must handle.
-- Sakila already has composite keys (film_actor, film_category), a reference
-- cycle (store.manager_staff_id and staff.store_id), a BLOB (staff.picture),
-- triggers that write another table (film to film_text), views and stored
-- routines. All names and contact details are invented.

USE sakila;

-- A self-reference: who referred whom.
CREATE TABLE customer_referral (
    customer_id INT UNSIGNED PRIMARY KEY,
    referred_by INT UNSIGNED NULL,
    note VARCHAR(100),
    CONSTRAINT fk_referral_customer FOREIGN KEY (customer_id) REFERENCES customer (customer_id),
    CONSTRAINT fk_referral_referrer FOREIGN KEY (referred_by) REFERENCES customer_referral (customer_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO customer_referral (customer_id, referred_by, note)
SELECT customer_id,
       CASE WHEN customer_id % 5 = 1 THEN NULL ELSE customer_id - 1 END,
       'referral recorded by phone'
FROM customer
WHERE customer_id <= 40
ORDER BY customer_id;

-- A polymorphic reference with no declared foreign key, holding free text that
-- looks like personal data.
CREATE TABLE customer_note (
    note_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    subject_type ENUM('customer', 'film') NOT NULL,
    subject_id INT UNSIGNED NOT NULL,
    body TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'customer', customer_id, CONCAT('Asked to be called back on (617) 222-', LPAD(customer_id, 4, '0'))
FROM customer
WHERE customer_id <= 25;
INSERT INTO customer_note (subject_type, subject_id, body)
SELECT 'film', film_id, 'Requested by a customer'
FROM film
WHERE film_id <= 25;

-- A table with neither a primary key nor foreign keys.
CREATE TABLE audit_event (
    recorded_at DATETIME NOT NULL,
    entity VARCHAR(20) NOT NULL,
    entity_id INT NOT NULL,
    detail VARCHAR(100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT INTO audit_event (recorded_at, entity, entity_id, detail)
SELECT TIMESTAMP '2022-02-01 00:00:00' + INTERVAL rental_id HOUR, 'rental', rental_id, 'created'
FROM rental
WHERE rental_id <= 100;

-- A geometry column, which travels in MySQL's internal binary form.
CREATE TABLE store_location (
    store_id INT UNSIGNED PRIMARY KEY,
    location POINT NOT NULL SRID 0,
    CONSTRAINT fk_location_store FOREIGN KEY (store_id) REFERENCES store (store_id)
) ENGINE=InnoDB;
INSERT INTO store_location (store_id, location)
SELECT store_id, ST_PointFromText(CONCAT('POINT(', store_id, ' ', store_id * 2, ')'))
FROM store;

-- A table a spec is expected to exclude, and a view that depends on it.
CREATE TABLE dba_maint_runs (
    run_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    ran_at DATETIME NOT NULL
) ENGINE=InnoDB;
INSERT INTO dba_maint_runs (ran_at) VALUES ('2022-03-01 00:00:00');
CREATE VIEW dba_recent_runs AS SELECT run_id FROM dba_maint_runs;

-- The test user reads Sakila and makes scratch databases of its own.
GRANT ALL ON *.* TO 'datadouble'@'%';
