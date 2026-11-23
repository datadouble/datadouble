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
    --job build,promote,prune,lock --spec <spec> --runner <runner> --work <dir>
```

Generate `promote` with it, so it follows a successful build. `lock` is a
manual job on the same runner, with only the sources' and `notify`'s
secrets: it re-locks
the catalog and publishes the new lock and its changes for review, never
committing them (see `datadouble-spec`). Add `--env
NAME` for credentials an adapter reads from the environment rather than the
spec, such as `AWS_ACCESS_KEY_ID` for an S3 store.

If the team's Jenkins agents are pods of the Kubernetes plugin, often with no
Docker, add `--agent kubernetes`. Then `--runner` names the production
namespace, as for Argo CD, and `--cloud` the plugin's cloud (its default
otherwise). The namespace must be production's alone, never shared with the
non-production side. `build` needs the spec's `runtime: kubernetes` on the
claim `datadouble`, with no `namespace` (engine pods then start in the job's
own namespace, so the same spec serves the non-production side) or this one,
and every path the spec names
(stores, the work directory) under the runtime's `mount`, since the job's pod
mounts that claim there as the engine pods do; `ci init` refuses otherwise.
Change the spec with `datadouble-spec` first if it uses Docker. Without
`--agent`, Jenkins jobs use agents with Docker.

## Tell the person

- The variables to define as secrets on the production side, from the
  response's `environment`, and its `instructions`.
- What the runner's machine needs: Docker, and every path the spec names,
  existing and owned by the engine's user (`999:999` for PostgreSQL); on
  Kubernetes, one PersistentVolumeClaim holding them. With Kubernetes agents,
  the `instructions` also list separate namespaces per side, the Kubernetes
  plugin's cloud with the Role its credentials need (pods, `pods/exec`,
  `pods/log`, events), the `datadouble` claim (`ReadWriteMany`, or
  `ReadWriteOnce` with the runtime's `node`) and the RBAC its service
  account needs to run engine pods.
- Where Docker is not `docker` with its socket at `/var/run/docker.sock`
  (installed outside `PATH`, or another Docker-compatible daemon on another
  socket): the operator sets `DATADOUBLE_DOCKER` and
  `DATADOUBLE_DOCKER_SOCKET` in the runner's environment, or you generate
  again with `--docker <command>` and `--docker-socket <absolute path>`.
  Never edit the generated files for it. Rootless Docker is not supported.
  Argo CD's jobs and Jenkins' Kubernetes agents run without Docker and
  take neither.
- With the `kubernetes` runtime driving a cluster the job does not run in,
  the runtime's `kubeconfig` is a secret reference (`{env: NAME}`) to the
  kubeconfig's content, never the runner's own kubeconfig file; `ci init`
  lists `NAME` with the other secrets. It needs a token or client
  certificate, since the image has no exec credential plugins (EKS, GKE and
  AKS logins). Jobs running in that cluster need none.
- That the operator marks the runner inside the boundary
  (`DATADOUBLE_BOUNDARY=inside` in its environment, or the
  `datadouble-boundary` ConfigMap on Kubernetes, which is also the only mark
  a Jenkins Kubernetes agent's pod reads). Until then every run is
  refused with `outside_boundary`; never set it yourself.
- Error messages name tables, columns, constraints and other objects, but
  show data values as `…` (`Key (email)=(…) already exists`, `Duplicate
  entry '…' for key 'users.email'`), and so is any quoted string the engine
  adapter cannot tell is an object's name, or any quoted string at all in a
  message a trigger or procedure raised. A `…` is never something to ask
  the person to reveal. When a build fails with `<tool> exited with status
  N: ...` and N is 125, 126 or 127, the message is the container runtime's
  own and is shown whole: 125 means `docker run` failed before the tool
  started (an image that is not published or could not be pulled, a
  registry's rate limit, an option the daemon refused), 126 and 127 that the
  command could not run or was not found in the image.
- Keyless signing uses the CI system's identity token; GitHub workflows get
  `id-token: write` for it. In the spec:

    ```yaml
    signing:
      cosign:
        keyless: true
    ```
