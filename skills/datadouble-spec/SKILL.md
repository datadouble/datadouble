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
