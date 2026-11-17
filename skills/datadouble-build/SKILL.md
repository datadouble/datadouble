---
name: datadouble-build
description: Set up Datadouble's build job, which plans, extracts, anonymizes, verifies and stores a golden image on a runner inside the production boundary, in GitHub Actions, GitLab CI, Jenkins or Argo CD. Use when adding or changing the scheduled build of a spec's images.
---

# The build job

Follow `../RULES.md` throughout. `build` reads production data, so everything
here happens inside the production boundary.

## What it needs

- A spec that plans (`datadouble-spec`).
- A runner inside the boundary: a self-hosted runner's label, GitLab runner
  tags, a Jenkins agent label, or a Kubernetes namespace. Refuse a vendor's
  hosted runner (rule 5); `ci init` refuses them too.
- A work directory in production scratch space that outlives each job, so a
  retried run resumes its generation (`--retry-key`, decision 100).

## Generate it

```
datadouble ci init --system <github-actions|gitlab|jenkins|argocd> \
    --job build,promote,prune --spec <spec> --runner <runner> --work <dir>
```

Generate `promote` with it, so it follows a successful build. Add `--env
NAME` for credentials an adapter reads from the environment rather than the
spec, such as `AWS_ACCESS_KEY_ID` for an S3 store.

## Tell the person

- The variables to define as secrets on the production side, from the
  response's `environment`, and its `instructions`.
- What the runner's machine needs: Docker, and every path the spec names,
  existing and owned by the engine's user (`999:999` for PostgreSQL); on
  Kubernetes, one PersistentVolumeClaim holding them.
- That the operator marks the runner inside the boundary
  (`DATADOUBLE_BOUNDARY=inside` in its environment, or the
  `datadouble-boundary` ConfigMap on Kubernetes). Until then every run is
  refused with `outside_boundary`; never set it yourself.
- Keyless signing (`signing: {cosign: {keyless: true}}`) uses the CI system's
  identity token; GitHub workflows get `id-token: write` for it.
