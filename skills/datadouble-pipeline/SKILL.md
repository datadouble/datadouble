---
name: datadouble-pipeline
description: Set up a whole Datadouble pipeline in a team's repositories, from the spec to the jobs on both sides of the production boundary (build, promote and lock inside it; fetch, provision, release, prune and activate outside). Use when someone wants Datadouble set up end to end, or asks where to start.
---

# A whole Datadouble pipeline

Follow `../RULES.md` throughout. This skill runs the others in pipeline
order; each also works alone.

## Ask everything up front

Inspect first (rule 3), then ask in one round for what is missing:

- The source: engine, and the environment variable its connection reads.
- The CI/CD system on each side of the boundary (they can differ): GitHub
  Actions, GitLab CI, Jenkins or Argo CD.
- The runner (label, tags, agent label or namespace) inside the production
  boundary, and the one outside it.
- The build's work directory in production scratch space.
- Stores: where builds are kept, and the export store non-production reads,
  and how non-production reaches it: the same directory or bucket, or a web
  server serving it (the read-only `http` store; see `datadouble-fetch`).
- Copies: a pool kept ready, or copies made on demand per pull request, and
  where they live.

## Then, in order

1. **The spec** with `datadouble-spec` (and `datadouble-files` if file trees
   come along). It must plan cleanly before anything else.
2. **Production:** `datadouble-build`, then `datadouble-promote`, generated
   together: `datadouble ci init --system <s> --job build,promote,prune,lock
   ...`. Without a shell inside the boundary, the first lock comes from the
   manual `lock` job, reviewed and committed by the person before `build`
   runs (`datadouble-spec`).
3. **Non-production:** `datadouble-fetch`, `datadouble-provision`,
   `datadouble-release`, `datadouble-prune` and `datadouble-activate`,
   generated together in a second `ci init`.

## Finish with one summary

- Every file `ci init` wrote, per side.
- Every variable each side must define as a secret, and where (repository or
  environment secrets, masked CI/CD variables, Jenkins credentials, a
  Kubernetes Secret), from the `environment` each `ci init` returned.
- The setup steps each `ci init` listed (`instructions`), such as GitLab's
  pipeline schedules.
- The schedules, in UTC, and what triggers what; `lock` runs only by hand.
- The operator's own step: marking the production runner inside the boundary.
