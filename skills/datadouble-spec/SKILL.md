---
name: datadouble-spec
description: Write or change a Datadouble spec, the YAML that says what to copy from a source and how to make it safe - source, subset, excludes, anonymization, hooks and verification - and check it with datadouble plan. Use for a new spec, a strict-mode failure, a Jailer model to import, or questions about what a spec does.
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
  value into the spec.

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

## Extensions, plugins and modules

The lock lists the add-ons the source uses beyond its server (its
requirements, such as PostgreSQL extensions), each at the source's version.
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

