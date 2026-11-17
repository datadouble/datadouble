# datadouble-argocd

The Argo CD CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/), and the Helm chart
(`chart/`) that runs Datadouble's jobs on Kubernetes.

## Writing Applications

```
datadouble ci init --system argocd --job build,promote,prune \
    --spec datadouble/shop.yml --runner datadouble-prod --work /srv/datadouble/work
datadouble ci init --system argocd --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner datadouble-dev
```

`--runner` names the namespace the jobs run in. Each call writes one Argo CD
Application, `argocd/datadouble-<spec>-<side>.yaml`, which installs the chart
from this repository at the release's tag. Its values hold the jobs and a
copy of the spec and its catalog lock, so generate it again after changing
either. The response lists what to set up:

- **A PersistentVolumeClaim** named `datadouble` in the namespace. Every path
  the spec names (stores, staging, copies, the work directory) lives on it,
  mounted at the same path in every pod, as the `kubernetes` runtime needs,
  so they must share one directory, which `ci init` checks.
- **A Secret,** `datadouble-<spec>`, holding the environment variables the
  jobs read secrets from, one key each. Each job reads only its own.
- **The boundary mark,** in production namespaces only: a ConfigMap named
  `datadouble-boundary` with `DATADOUBLE_BOUNDARY=inside` (decision 98).

## Jobs

Each job is a CronJob, `datadouble-<spec>-<side>-<job>`, scheduled in UTC, or
suspended when it has none. Start one by hand with `kubectl create job
--from=cronjob/...`. A job that follows another runs as a later step of that
job's pod, so `promote` runs after a successful `build`, and a pool's
`provision` after `fetch`; each also has its own CronJob for running alone.
`activate` reads its generation, and `prune` its dry run, from the values
(`generation`, `dryRun`).

A retried pod keeps its Job's name, which is the run's retry key, so a build
that fails part way resumes on Kubernetes' own retry.

## On-demand copies

A request's copies are Argo CD hooks. An Application whose values set
`owner` renders only those: a PreSync Job that makes the owner's copies, and
a PostDelete Job that releases them when the Application is deleted. The
hooks use the non-production Application's service account and spec, so a
request's Application goes in the same namespace, with the same `name`
value. Make one per pull request with an ApplicationSet:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: ApplicationSet
metadata: {name: datadouble-shop-requests, namespace: argocd}
spec:
  generators:
    - pullRequest:
        github: {owner: example, repo: shop}   # or your source host's generator
  template:
    metadata:
      name: 'datadouble-shop-pr-{{number}}'
      finalizers: [resources-finalizer.argocd.argoproj.io]
    spec:
      # The non-production Application's spec, with:
      #   source.helm.releaseName: 'shop-pr-{{number}}'
      #   source.helm.valuesObject.owner: 'pr-{{number}}'
```

## Outcomes

A Kubernetes Job succeeds or fails; it has no warning state. A warning
succeeds and says so in the log and in the container's termination message,
which holds the outputs (`outcome`, `generation`, `error`). Nothing is kept
anywhere else.
