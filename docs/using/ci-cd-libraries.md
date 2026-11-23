# CI/CD libraries

`datadouble ci init` writes a team's jobs for one side of the production
boundary, in its CI/CD system's own form. The jobs are the same on every
system, decided by Datadouble rather than the library (decision 100):

| Job | Side | Runs | Started by |
|---|---|---|---|
| build | production | plan through store | weekly schedule, or by hand |
| promote | production | promote | a successful build, or by hand |
| prune | either | prune | daily schedule |
| fetch | non-production | fetch | every 15 minutes |
| provision | non-production | replicate | hourly and after fetch (pool), or a pull request (on demand) |
| release | non-production | release | a closed pull request, and a daily sweep |
| activate | non-production | activate a chosen generation | by hand |
| lock | production | lock, publishing the new lock and its changes for review | by hand |

```
datadouble ci init --system <github-actions|gitlab|jenkins|argocd> \
    --job build,promote,prune,lock --spec datadouble/shop.yml \
    --runner <inside the boundary> --work /srv/datadouble/work
datadouble ci init --system <...> --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner <outside it>
```

The response lists the files written, the environment variables to define
as secrets on that side (each job gets only its own), and anything the
system's configuration cannot hold (`instructions`). Generate again after
changing the spec or the options, rather than editing the files.

The `lock` job re-locks the catalog after a schema change, on the production
runner, since reading the source needs the boundary. It never commits: it
publishes the new `catalog.lock` and `changes.txt`, what changed since the
committed lock, as the job's artifact (on Argo CD, in the job's log).
Someone checks every new or changed column for personal data, then commits
the lock beside the spec. A lock holds names, types and versions, never
values (decision 123).

Each system's details are in its adapter's README:

- [GitHub Actions](https://github.com/datadouble/datadouble/tree/main/ci/github-actions):
  a workflow per job, using the action in `ci/github-actions`.
- [GitLab CI](https://github.com/datadouble/datadouble/tree/main/ci/gitlab):
  a file of jobs per side, with a job template pinned by SHA-256, pipeline
  schedules and merge request environments.
- [Jenkins](https://github.com/datadouble/datadouble/tree/main/ci/jenkins):
  a Jenkinsfile per job, with the shared library loaded at the release's tag.
- [Argo CD](https://github.com/datadouble/datadouble/tree/main/ci/kubernetes):
  an Application per side, installing the Helm chart, with hooks for
  requests' copies.

## On the runners

- **Inside the production boundary,** the operator marks each runner with
  `DATADOUBLE_BOUNDARY=inside` (or, on Kubernetes, a `datadouble-boundary`
  ConfigMap). Unmarked machines, and every vendor's hosted runner, are
  refused before anything connects (decisions 19 and 98).
- **Docker,** with every path the spec names existing and owned by the
  engine's user; jobs run the pinned Datadouble image as that user. On
  Kubernetes, one PersistentVolumeClaim holds those paths.
- **Outcomes** map to the system's own: a warning is a GitHub warning
  annotation, a GitLab allowed failure, an unstable Jenkins build, or a
  succeeding Kubernetes Job that says so in its log.
