-- Copyright (c) 2026- Tight Line LLC
-- SPDX-License-Identifier: MIT

-- Pagila's objects belong to a "postgres" role. The superuser here is
-- "datadouble", so the role is created first; working databases restore
-- without owners, so it never needs to exist there.
CREATE ROLE postgres;
