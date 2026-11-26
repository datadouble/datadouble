# CI/CD libraries

This page shows how to run Datadouble's jobs from GitHub Actions, GitLab CI,
Jenkins or Argo CD. You don't write the pipeline configuration yourself:
`datadouble ci init` writes it from your spec, in your CI/CD system's own form.

Datadouble's jobs run on two sides of the **production boundary**. Jobs that
read real personal data (building an image, promoting it, re-locking the
catalog) run inside production. Jobs that make copies for developers and tests
run outside it. So you generate, and set up, each side separately. The
[CI/CD integration](../ci-cd.md) page explains why, and how the jobs work; this
page covers the steps:

1. [Generate the jobs](#generate-the-jobs) for each side.
2. [Set up the runners](#set-up-the-runners) the jobs run on.
3. [Re-lock the catalog](#re-lock-after-a-schema-change) after a schema
   change.
4. [Get notified](#get-notified) when a job fails, warns or recovers.

## Generate the jobs {#generate-the-jobs}

Run `datadouble ci init` once for each side of the boundary. Here is a typical
pair of commands:

```
datadouble ci init --system <github-actions|gitlab|jenkins|argocd> \
    --job build,promote,prune,lock --spec datadouble/shop.yml \
    --runner <inside the boundary> --work /srv/datadouble/work
datadouble ci init --system <...> --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner <outside it>
```

The options:

- `--system` is your CI/CD system.
- `--job` lists the jobs to write. All of them must belong to one side.
- `--spec` is the spec the jobs run.
- `--runner` is where the jobs run: a runner tag or label, or on Kubernetes
  the side's namespace. The production side always needs one, and so does
  either side on GitLab, Jenkins and Argo CD.
- `--work` is `build`'s work directory, an absolute path in production
  scratch space. It is kept between runs, so a retried build resumes.
- `--side` sets the side when you generate `prune` on its own, since `prune`
  runs on either side.

Some jobs need a section of the spec: `promote` needs `promote`, `fetch` needs
`fetch`, and `provision` and `release` need `replicate`. `replicate` also sets
whether copies are kept in a pool or made on demand.

The response lists:

- `files`: the files written;
- `environment`: the environment variables to define as secrets on that side.
  Each job gets only its own;
- `instructions`: anything the system's configuration cannot express, such as
  GitLab's pipeline schedules. Do each step it lists.

Commit the files. After changing the spec or the options, generate again
rather than editing the files.

### The jobs you get {#the-jobs-you-get}

The jobs are the same on every system. Datadouble decides them, not the
library (decision 100). Schedules are in UTC.

| Job | Side | Runs | Started by |
|---|---|---|---|
| build | production | plan through store | weekly schedule (Mondays at 02:00), or by hand |
| promote | production | promote | a successful build, or by hand |
| lock | production | lock, publishing the new lock and its changes for review | by hand |
| prune | either | prune | daily schedule (04:00), or by hand |
| fetch | non-production | fetch | every 15 minutes, or by hand |
| provision | non-production | replicate | hourly, after fetch, or by hand (pool); a pull or merge request opening (on demand) |
| release | non-production | release | a daily sweep of expired copies (03:30), or by hand; also a closed pull or merge request (on demand) |
| activate | non-production | activate a chosen generation | by hand |

### What each system gets {#what-each-system-gets}

Each system's details are in its adapter's README:

- [GitHub Actions](https://github.com/datadouble/datadouble/tree/main/ci/github-actions):
  a workflow per job, using the action in `ci/github-actions`.
- [GitLab CI](https://github.com/datadouble/datadouble/tree/main/ci/gitlab):
  a file of jobs per side, with a job template pinned by SHA-256, pipeline
  schedules and merge request environments.
- [Jenkins](https://github.com/datadouble/datadouble/tree/main/ci/jenkins):
  a Jenkinsfile per job, with the shared library loaded at the release's tag,
  on agents with Docker or, with `--agent kubernetes`, as pods of the
  Kubernetes plugin.
- [Argo CD](https://github.com/datadouble/datadouble/tree/main/ci/kubernetes):
  an Application per side, installing the Helm chart, with hooks for
  requests' copies.

## Set up the runners {#set-up-the-runners}

The generated jobs expect a few things of the machines they run on. Most
setups need only the first two.

### Mark the production runners {#mark-the-production-runners}

Production jobs refuse to run on a machine nobody has marked as inside the
production boundary. So mark each runner inside the boundary once, where the
runner is configured:

- On a runner or agent, set `DATADOUBLE_BOUNDARY=inside` in its environment.
- On Kubernetes, create a ConfigMap named `datadouble-boundary`, holding
  `DATADOUBLE_BOUNDARY=inside`, in the production namespace.

Unmarked machines, and every vendor's hosted runner, are refused before
anything connects (decisions 19 and 98). See
[where jobs run](../ci-cd.md#where-jobs-run) for the rules.

### Give the runners Docker {#give-the-runners-docker}

Jobs run the pinned Datadouble image with Docker, as the engine's user. So
each runner needs:

- Docker;
- every path in the spec, existing and owned by the engine's user.

On Kubernetes, one PersistentVolumeClaim holds those paths.

### Use another Docker command or socket {#another-docker}

If Docker is installed outside `PATH`, or you use another Docker-compatible
daemon on another socket, tell the jobs where to find it. Either:

- set these in the runner's environment:
    - `DATADOUBLE_DOCKER`, the command (`docker` by default);
    - `DATADOUBLE_DOCKER_SOCKET`, the daemon's socket
      (`/var/run/docker.sock` by default). A `unix://` `DOCKER_HOST` also sets
      it.
- or pass `ci init --docker` and `--docker-socket`, which write them into the
  generated configuration (decision 126).

Inside the Datadouble container, the socket is always mounted at
`/var/run/docker.sock`. Rootless Docker is not supported. Argo CD's jobs and
Jenkins' Kubernetes agents are pods without Docker, and refuse both settings.

### Run jobs as Kubernetes pods (Jenkins) {#kubernetes-agents}

With `--agent` set to `kubernetes`, each job runs as a pod of the pinned image,
as the engine's user, beside the engine pods. Only Jenkins supports this.
It works like Argo CD:

- `--runner` sets the side's namespace, and the pod mounts that namespace's
  `datadouble` claim.
- `--cloud` sets the Jenkins Kubernetes plugin's cloud. Without it, the
  plugin's default cloud is used.

First, the spec. `ci init` refuses a spec that breaks either rule:

- For jobs that start engines, use the spec's `kubernetes` runtime on the
  `datadouble` claim, with every path in the spec under the runtime's
  `mount`.
- Leave the runtime's `namespace` out. Engine pods then start in the
  namespace of the job's own pod, so one spec serves both sides. If you do
  set it, it must be the side's `--runner`.

Then the cluster. `ci init` lists each of these steps in its `instructions`:

1. Give each side its own namespace, ideally its own cluster.
2. Set up the plugin's cloud, with the Role the plugin documents (pods,
   `pods/exec`, `pods/log`, events).
3. Create the `datadouble` PersistentVolumeClaim in the namespace, owned by
   the engine's user.
4. Create the pods' service account, `datadouble-<spec>-<side>`, with RBAC
   for engine pods: pods, their logs, exec and port forwarding, Secrets, and
   reading PersistentVolumeClaims. On the non-production side, `k8s-pv` also
   needs PersistentVolumes.
5. Mark the production namespace with the `datadouble-boundary` ConfigMap.
   On Kubernetes agents, this is the only boundary mark that counts.

## Re-lock after a schema change {#re-lock-after-a-schema-change}

When the source's schema changes, strict mode needs a new catalog lock. The
`lock` job makes one. It runs on the production runner, since reading the
source needs the boundary.

1. Start the `lock` job by hand.
2. Download the job's artifact. It contains the new lock, named after the spec (such as `shop.lock`), and
   `changes.txt`: what changed since the committed lock. On Argo CD, both are
   in the job's log instead.
3. Check every new or changed column for personal data.
4. Commit the new lock beside the spec.

The job never commits for you. Publishing the lock is safe: a lock contains
names, types and versions, never values (decision 123). See
[the lock job](../ci-cd.md#the-lock-job) for the details.

## Get notified {#get-notified}

The spec's `notify` section configures notifications for jobs that fail,
warn or recover. It lives in the spec rather than in `ci init`'s options, so generating the
jobs again keeps it:

```yaml
notify:
  when: [failure, warning, recovery]   # default: [failure]
  to:
    - webhook:
        url:
          env: ALERTS_WEBHOOK
    - email:
        to: [data-team@example.com]
```

- **Webhooks** work on every system. Datadouble posts a JSON body with the
  spec's name, the job, its outcome, the generation, the error code and the
  run's page, never data values or secrets. Its `text` field puts those in
  one line. For a chat room, add `format: chat` to the webhook, and
  Datadouble posts only that line. Google Chat, Slack, Mattermost and
  Rocket.Chat incoming webhooks accept it. Google Chat refuses any other
  key, so it needs `chat`. The URL is a secret reference, so `ci init`
  lists its variable with the others.
- **Recovery** is a success after a run that failed, warned or was stopped.
  A webhook gets one post for it, with `"recovered": true` and a `text`
  that says the job recovered, and nothing for later successes. Each job
  keeps a small record of its last outcome for this, in the work directory
  on the production side and in `fetch.staging` on the other, so `ci init`
  then needs `--work` for every production job. The first run after you add
  `recovery` has no record, so it posts nothing.
- **Email** uses the system's own mechanism:
    - Jenkins: a `mail` step in the Jenkinsfile's `post` section, with
      `fixed` for recoveries;
    - GitLab: the Pipeline status emails integration, set up by hand as
      `instructions` says. It mails no recoveries: GitLab's Fixed pipeline
      notification mails the pipeline's author, if they turn it on, and
      `instructions` says so;
    - GitHub: none a workflow can send. GitHub mails a failed run only to
      whoever started it, and no recovery at all, so `instructions` suggests
      a webhook to a service that sends mail;
    - Argo CD: refused, since it has no mail.
- **A notification that fails** is a warning in the job's log. It never
  changes the job's outcome.
- **A job that fails before Datadouble starts** (an image that cannot be
  pulled, a missing path) posts no webhook, and leaves no record, so the
  next success is not counted as a recovery from it. The system's own failure
  notifications cover it, and on Argo CD the cluster's monitoring of failed
  Jobs.

See [notifications](../ci-cd.md#notifications) for how each system handles
them.

## Read a job's outcome {#outcomes}

A job ends in success, warning or failure. A warning means the image was
built and verified, but something needs an operator's attention. Each system
shows a warning in its own way:

| System | A warning appears as |
|---|---|
| GitHub Actions | A warning annotation on a passing step |
| GitLab CI | An allowed failure |
| Jenkins | An unstable build |
| Argo CD | A succeeding Kubernetes Job that says so in its log |
