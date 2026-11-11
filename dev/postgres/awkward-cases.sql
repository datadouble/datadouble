-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Cases Pagila lacks that the subset planner and verification must handle.
-- Pagila already has composite keys (film_actor, film_category), a reference
-- cycle (store.manager_staff_id and staff.store_id), a partitioned table
-- (payment), a virtual generated column (film.length_hours), domains, an enum
-- and an extension type. All names and contact details are invented.

-- A self-reference: who referred whom.
CREATE TABLE public.customer_referral (
    customer_id integer PRIMARY KEY REFERENCES public.customer (customer_id),
    referred_by integer REFERENCES public.customer_referral (customer_id),
    note text
);
INSERT INTO public.customer_referral (customer_id, referred_by, note)
SELECT customer_id,
       CASE WHEN customer_id % 5 = 1 THEN NULL ELSE customer_id - 1 END,
       'referral recorded by phone'
FROM public.customer
WHERE customer_id <= 40;

-- A polymorphic reference with no declared foreign key, holding free text that
-- looks like personal data.
CREATE TABLE public.customer_note (
    note_id serial PRIMARY KEY,
    subject_type text NOT NULL CHECK (subject_type IN ('customer', 'film')),
    subject_id integer NOT NULL,
    body text
);
INSERT INTO public.customer_note (subject_type, subject_id, body)
SELECT 'customer', customer_id, 'Asked to be called back on (617) 222-' || lpad(customer_id::text, 4, '0')
FROM public.customer
WHERE customer_id <= 25;
INSERT INTO public.customer_note (subject_type, subject_id, body)
SELECT 'film', film_id, 'Requested by a customer'
FROM public.film
WHERE film_id <= 25;

-- A table with neither a primary key nor foreign keys.
CREATE TABLE public.audit_event (
    recorded_at timestamptz NOT NULL DEFAULT now(),
    entity text NOT NULL,
    entity_id integer NOT NULL,
    detail text
);
INSERT INTO public.audit_event (recorded_at, entity, entity_id, detail)
SELECT timestamptz '2022-02-01 00:00:00+00' + make_interval(hours => rental_id),
       'rental', rental_id, 'created'
FROM public.rental
WHERE rental_id <= 100;

-- Tables a spec is expected to exclude.
CREATE SCHEMA dba;
CREATE TABLE dba.dba_maint_runs (run_id serial PRIMARY KEY, ran_at timestamptz NOT NULL);
INSERT INTO dba.dba_maint_runs (ran_at) VALUES (timestamptz '2022-03-01 00:00:00+00');
CREATE VIEW dba.recent_runs AS SELECT run_id FROM dba.dba_maint_runs;
