# datadouble-argocd

The Argo CD CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/), and the Helm chart
(`chart/`) that runs Datadouble's jobs on Kubernetes.

## Writing Applications

```
datadouble ci init --system argocd --job build,promote,prune,lock \
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
  so they must share one directory, which `ci init` checks. Leave the spec's
  `runtime.kubernetes.namespace` out: the engine pods then start in the
  namespace of the job's pod, each side's own, so one spec serves both
  sides. A namespace that is set must be `--runner` for jobs that start
  engines, or `ci init` refuses.
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
A Job runs a failed pod once more (`backoffLimit: 1`), which resumes a
failed build's generation, but a failed follower fails the Job at once
(`podFailurePolicy`): running the pod again would run its leader again, so
a build whose `promote` was refused would build, and notify, twice. Run the
follower's own CronJob once its problem is fixed.
`activate` reads its generation, and `prune` its dry run, from the values
(`generation`, `dryRun`).

A retried pod keeps its Job's name, which is the run's retry key, so a build
that fails part way resumes on Kubernetes' own retry.

## Re-locking the catalog

The `lock` job (`--job lock`, on the production side with `build`) has no
schedule; start it with `kubectl create job
--from=cronjob/datadouble-<spec>-production-lock`. It runs `datadouble lock
--output datadouble-lock`, into an `emptyDir`, against the lock in the
Application's values. Kubernetes keeps no job artifacts, so the job prints
the new `catalog.lock` and `changes.txt` to its log (`kubectl logs`), each
after a `==> <path> <==` line, and its termination message carries
`fingerprint` and `changes`, their number. It never commits: check every new
or changed column for personal data, commit `catalog.lock` beside the spec,
and generate the Application again so its values carry the new lock. A lock
holds names, types and versions, never values (decision 123).

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
which holds the outputs (`outcome`, `generation`, `error`; `fingerprint` and
`changes` for `lock`). Nothing is kept anywhere else.

## Notifications

The spec's `notify` section says who hears about a job that fails or warns.
Each step names its job (`--ci-job`), so Datadouble posts the spec's webhooks
itself after each step's command; a Job has no web page, so `run_url` is
null. A Job runs a failed pod once more (`backoffLimit: 1`), and each attempt
posts, so a failure usually notifies twice; a failed follower is not run
again (see [Jobs](#jobs)), so it, and the leader before it, notify once. Put
the webhook's variable in the side's Secret like the others. A webhook that cannot be reached is a warning line in the log; the Job's
result is unchanged.

Kubernetes Jobs have no mail mechanism, so `ci init` fails for `notify`'s
email, by name; list a webhook to a service that mails the team instead. A
Job that fails before Datadouble starts (an image that cannot be pulled, a
missing claim or Secret, a pod stopped at its deadline) posts nothing, so
`ci init` asks you to alert on failed Jobs in the namespace with the
cluster's monitoring, such as kube-state-metrics' `kube_job_status_failed`.
