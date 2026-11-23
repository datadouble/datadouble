---
name: datadouble-spec
description: Write or change a Datadouble spec, the YAML that says what to copy from a source and how to make it safe - source, subset, excludes, anonymization, hooks and verification - and check it with datadouble plan. Use for a new spec, a strict-mode failure, a Jailer model to import, putting a connection's password, key or other credential into a spec (never pass the secret itself, not even to this skill), or questions about what a spec does.
---

# The Datadouble spec

Follow `../RULES.md` throughout.

## Start from what exists

- `datadouble schema` prints the JSON Schema every spec is checked against;
  read it rather than guessing field names.
- A Jailer extraction model translates with `datadouble import-jailer --spec
  <spec> --model <model.jm> --datamodel <dir> --output <file>`; it writes the
  native settings and the catalog lock, and lists what it could not carry
  over.
- Secrets are references: `{env: NAME}` or `{file: /path}`. Never write a
  value into the spec. The one exception is a copy account's password, below.

## Check it, without production data

`datadouble plan --spec <spec>` reads only the catalog and checks the spec
against it and its lock (`catalog.lock` beside the spec). Planning reads the
source, so it runs only where the operator marked the machine inside the
production boundary. Outside it, generate the `build` job (with
`datadouble-build`) and let its first run plan on the production runner; the
person can share the plan's output, which names tables and columns but never
values.

## When strict mode fails

`strict: columns` is the default. A new table or column fails the plan with
each object and the ways to resolve it: anonymize it, exclude it, or accept
it in the lock once someone has checked it holds no personal data. Show the
person those options and let them choose (rule 4). `datadouble lock` records
the current catalog after they have.

## When tables have no rows selected

A kept table that no seed, `follow: all` or followed edge reaches fails the
plan by default (`subset.unselected: fail`). Each one is a choice about what
the copy holds, so show the person the options and let them pick:

- seed it, or seed many at once with a glob (`"*": "TRUE"` in SQL engines);
- follow a reference that reaches it;
- make it `structure_only`, or exclude it;
- `unselected: all` to copy every row of such tables (any engine), or
  `unselected: none` to copy them without rows, with a warning.

A Jailer subset already behaves as `none`; change the Jailer model instead.

## Extensions, plugins and modules

The lock lists the add-ons the source uses beyond its server (its
requirements: PostgreSQL extensions, MySQL plugins, Redis modules, search
plugins, Oracle index types), each at the source's version where it has one.
Strict mode fails the plan when one is new, removed or upgraded, and a build
fails with `requirement_unmet` when the working image lacks one.

- **Look before you ask.** The lock (`catalog.lock`) lists the requirements;
  the engine adapter's README lists images that provide common ones.
- **Suggest an image first.** Propose a `source.image` built on the source's
  server release that has the add-on, such as `postgis/postgis:18-3.6`. Some
  add-ons also need `source.server` settings; for PostgreSQL,
  `shared_preload_libraries`.
- **Leaving one out is the person's decision.** `source.omit` drops a
  requirement from the copies, and anything that uses it must be excluded
  too. Explain that, and add it only when the person asks (rule 4). Managed
  services' own extensions, such as `aws_s3` on Amazon RDS, are the usual
  case.
- **Columns of an add-on's type** (`geometry`, `vector`, `hstore`) have the
  kind `other`: `faker` does not apply, so suggest `null`, `expression` or
  keeping the column if it holds no personal data.

## Accounts for copies

`package.accounts` gives physical images accounts that log in over TCP, for
consumers that serve a volume copy with the engine's official image (whose
password variables apply only to a new data directory). Read the engine
adapter's README before adding one.

- **Physical images carry them, and so every copy of one,** volume and
  instance copies alike. Logical images never do. A build that makes only
  logical images refuses `package.accounts`, as does an engine whose adapter
  can't create accounts (MySQL and PostgreSQL can).
- **Passwords may be plain text,** since copies hold no personal data; this
  is the only password a spec may hold as text (rule 6). A team that prefers
  a reference uses `{env: NAME}` or `{file: /path}`, and `ci init` passes it
  to the build job. Every other credential stays a reference.
- **`privileges`:** `all` (the default) is every privilege with the grant
  option, a PostgreSQL superuser. `read` is `SELECT` on the image's data,
  without other accounts' password hashes, but not strictly read-only: it
  can change its own password, and on PostgreSQL it keeps what `PUBLIC` has
  (temporary tables, running functions, and before version 15 creating
  objects in `public`). Suggest `read` for reporting tools and anything that
  only reads, and say what it still allows.
- **Say what `all` means.** An `all` account on a copy others can reach can
  run commands in the consumer's container, for example PostgreSQL's
  `COPY ... TO PROGRAM`. Tell the person before adding one.
- **PostgreSQL images trust local logins.** Anything inside the consumer's
  container, a sidecar or a `kubectl port-forward` logs in as `postgres`
  without a password, accounts or not. An instance copy also resets
  `postgres`'s password to its generated one, so a declared `postgres`
  account's password holds on volume copies only.
- **Names the engine can't use fail the plan:** duplicates; MySQL names over
  32 characters, `datadouble` and `datadouble-build`; PostgreSQL names over
  63 bytes, starting with `pg_`, or reserved (`public`, `none`,
  `current_user`, `session_user`, `current_role`), and `postgres` with
  `privileges: read`. An existing user of the same name, such as `root` or
  `postgres`, gets the declared password.
