# datadouble-jenkins

The Jenkins CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It has two parts:

- **The shared library in this directory** (`vars/datadouble.groovy` and
  the scripts in `resources/datadouble/`), whose `datadouble` step runs one
  `datadouble` command in the Datadouble image: with `docker run` on an agent
  with Docker (`run.sh`), or in the Datadouble container of a Kubernetes
  agent's pod (`pod.sh`).
- **The Python package,** which writes Jenkinsfiles for `datadouble ci init
  --system jenkins` and, at run time, reports a response's outputs and
  outcome to the step.

## Writing Jenkinsfiles

```
datadouble ci init --system jenkins --job build,promote,prune,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system jenkins --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

Each job gets a declarative Jenkinsfile, `jenkins/datadouble-<spec>-<job>.Jenkinsfile`,
for a Pipeline job of the same name. It loads the shared library from this
repository at the release's tag (`library` with `modernSCM` and
`libraryPath: 'ci/jenkins/'`), so Jenkins needs no global library set up,
only the Pipeline: Groovy Libraries and Git plugins. The response lists:

- **The Pipeline jobs to create,** each running its Jenkinsfile from your
  repository. Build each once: Jenkins reads a Jenkinsfile's triggers and
  parameters when it runs.
- **The credentials** to add as Secret text, with IDs matching the
  environment variables the jobs read. `withCredentials` binds them, and
  Jenkins masks them in logs. The web form takes one line, so create a
  multi-line one, such as a kubeconfig, through the API or Configuration as
  Code.

Schedules are `cron` triggers in UTC. `promote` has an `upstream` trigger on
`build` that accepts an unstable build, and a pool's `provision` one on
`fetch`. Jobs never run two builds at once (`disableConcurrentBuilds`).

## On-demand copies and retries

Jenkins has no pull request events that every installation shares, so
`provision` and `release` take an `OWNER` parameter. Start them from a
request's own pipeline:

```groovy
build(job: 'datadouble-shop-provision', parameters: [string(name: 'OWNER', value: "pr-${env.CHANGE_ID}")])
```

`release` without an `OWNER` is the scheduled sweep of expired copies.

A retried Jenkins build is a new build, with a new `BUILD_TAG`. To resume a
`build` that failed part way, start it with `RETRY_KEY` set to the failed
build's `BUILD_TAG`, shown at the top of its log.

## Where jobs find the spec

By default each Jenkinsfile reads the spec, the specs it extends and its
lock from the build's checkout of this repository. `--spec-source` changes
that (see [where jobs find the spec](https://datadouble.tightlinesoftware.com/ci-cd/#where-jobs-find-the-spec)):

- `--spec-source mounted --spec-dir DIR`: on an agent with Docker, the
  spec's directory is `DIR` on the agent, mounted into each job at the same
  path. On Kubernetes agents, also name a ConfigMap you manage with
  `--spec-config-map NAME`, keyed by file name. The pod mounts it
  read-only at `DIR` in the Datadouble container.
- `--spec-source git --spec-repository URL --spec-ref REF --spec-dir DIR
  --spec-credential ID`: each Jenkinsfile starts with a `spec` stage that
  runs `checkout scmGit(...)` inside `dir('.datadouble-spec')`, with the
  credential `ID`: Username with password (an access token as the
  password), or SSH Username with private key for an SSH URL. The
  response's instructions name it. `REF` is a branch by its name, a tag as `refs/tags/<tag>`, or a commit
  by its full SHA. The Git plugin
  doesn't recognize a bare tag name.

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and archives the new lock (named after the spec, such as
`shop.lock`) and `changes.txt` as the build's artifacts. It never commits:
download them from the build's page, check every new or changed column in
`changes.txt` for personal data, then commit the lock beside the spec. A lock
contains names, types and versions, never values (decision 123).

## What a job needs on its agent

- The labels given to `--runner`.
- Docker, with its socket at `/var/run/docker.sock` unless the agent says
  otherwise (below), usable by the agent's user. The step runs the
  Datadouble image as the engine's user on the host's network, with the
  workspace and every absolute path in the spec mounted at the same path.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL).
- On agents inside the production boundary, `DATADOUBLE_BOUNDARY=inside` in
  the agent's environment (decision 98).

### Another Docker command or socket

The step runs the image with `docker` and the daemon at
`/var/run/docker.sock`. Where Docker is installed outside `PATH`, or the
daemon is another Docker-compatible one listening on another socket, set
`DATADOUBLE_DOCKER` (one program, a name or an absolute path) and
`DATADOUBLE_DOCKER_SOCKET` (an absolute path) in the agent's environment,
once for every job on the agent. Or pass `--docker` and `--docker-socket` to
`ci init`, which writes them into the Jenkinsfiles as the step's `docker`
and `dockerSocket`, where they take precedence over the agent's own
(decision 126).

Without `DATADOUBLE_DOCKER_SOCKET`, a `DOCKER_HOST` of the form
`unix://PATH` in the agent's environment names the socket. A socket named
either way is also the daemon the image is started on (`DOCKER_HOST`, and
`CONTAINER_HOST` for podman), so the image and the engines' containers share
one daemon. The socket is mounted at `/var/run/docker.sock` inside the
Datadouble container, and `DOCKER_HOST` there always points at it, even when
a job passes its own `DOCKER_HOST` on.

Rootless Docker is not supported: its user namespace maps the engine's user
to another one on the host, so neither the socket's group nor the files
shared at the same paths (decision 73) line up. The step reads the socket's
group with GNU `stat`, so the agent needs GNU coreutils, as Linux
distributions have.

## Kubernetes agents

Jenkins installations whose agents are pods of the Kubernetes plugin often
have no Docker, and the spec's `kubernetes` runtime needs Datadouble itself
in a pod that shares the engine pods' work claim. With `--agent kubernetes`
each Jenkinsfile declares its own pod instead of a label. As for Argo CD
(decision 103), `--runner` sets the side's namespace; `--cloud` sets the
plugin's cloud, and without it the pods start in the plugin's default cloud.
Agents with Docker stay the default (`--agent docker`).

```
datadouble ci init --system jenkins --agent kubernetes --job build,promote,prune \
    --spec datadouble/shop.yml --runner shop-production --cloud prod-k8s \
    --work /srv/datadouble/work
```

```groovy
agent {
    kubernetes {
        cloud 'prod-k8s'
        namespace 'shop-production'
        yaml '''
            apiVersion: v1
            kind: Pod
            spec:
              serviceAccountName: datadouble-shop-production
              containers:
              - name: jnlp
                ...
              - name: datadouble
                image: ghcr.io/datadouble/datadouble:v...
                securityContext: {runAsUser: 999, runAsGroup: 999, ...}
              ...
            '''
    }
}
```

- **Each side has its own namespace,** and the two sides must not share one;
  separate clusters are better still. A namespace's `datadouble` claim holds
  its side's files, and production's holds data extracted from the source.
  Generate each side with its own `--runner`.
- **The pod mounts its namespace's `datadouble` claim** where the side's paths
  are. Jobs that start engine software (`build`, `provision`, `release`) need
  the spec's `kubernetes` runtime on the claim `datadouble`, with every path
  in the spec (stores, staging, the work directory) under its `mount`,
  which is where the pod mounts the claim, the same path the engine pods
  use. Other jobs mount the claim at their paths' common directory, as Argo
  CD does. `ci init` refuses anything else. The pod keeps to the runtime's
  `node` when one is set, and uses its `image_pull_secrets`.
- **Leave the runtime's `namespace` out.** The engine pods then start in the
  namespace of the job's own pod, read from its service account, so one
  spec serves both sides, each in its own namespace. A runtime that names a
  namespace must name this side's `--runner`, so `ci init` refuses a spec
  naming production's namespace for the non-production jobs.
- **Only the Datadouble container runs as the engine's user** (decision 86).
  The plugin's agent container (`jnlp`) keeps its image's user, whose account
  and home an SCM checkout over SSH needs. The two share the workspace, so
  the agent container starts with umask `0000`, which leaves the directories
  it creates, including each `sh` step's control directory, writable by the
  Datadouble container. That workspace is the pod's own and goes with it.
  The override runs `/usr/local/bin/jenkins-agent`, as in the
  `jenkins/inbound-agent` image the plugin uses by default; a cloud with
  another agent image needs that path.
- **The step runs `datadouble` in the Datadouble container** with
  `container`, with secrets bound by `withCredentials` as in Docker mode.
- **The boundary mark is only the namespace's** `datadouble-boundary`
  ConfigMap, which the operator creates in production namespaces, as for the
  Helm chart. The pod mounts it as an optional volume and the step reads the
  file. It ignores every environment variable for the mark, since Jenkins
  lays its own environment (global variables, `withEnv`) over the
  container's, so a pod in an unmarked namespace is refused `build` and
  `promote` whatever Jenkins sets.
- **Engine pods see only the claim.** Anything an engine pod reads must be on
  it, not in the workspace.
- **There is no Docker,** so `--docker` and `--docker-socket` are refused.
  The rest is as on Docker agents: each step names its job for `notify`'s
  webhooks, `post` sends its email, and the `lock` job's new lock and changes
  are archived as the build's artifacts.

The response's `instructions` list what to set up:

- Separate namespaces for the two sides.
- The Kubernetes plugin and its cloud, whose credentials need a Role in the
  namespace with the rights the plugin's own documentation lists: `create`,
  `delete`, `get`, `list`, `patch`, `update` and `watch` on pods, `create`
  and `get` on `pods/exec` (the `container` step), `get`, `list` and `watch`
  on `pods/log`, and `watch` on events. `CLOUD_RULES` in the package holds
  them, and `ACCOUNT_RULES` the service account's below.
- The `datadouble` claim: `ReadWriteMany`, or `ReadWriteOnce` with the
  runtime's `node` set so every pod runs on that node, owned by the engine's
  user.
- The service account `datadouble-<spec>-<side>` the pods run as, with a Role
  allowing `get`, `list`, `watch`, `create`, `delete` and `patch` on pods,
  `pods/log`, `pods/exec`, `pods/portforward` and Secrets, and `get` and
  `list` on PersistentVolumeClaims; on the non-production side, a
  ClusterRole for PersistentVolumes as well, which the `k8s-pv` replicate
  adapter makes.
- On the production side, the `datadouble-boundary` ConfigMap, inside the
  production boundary only.

The conformance suite runs Kubernetes agents end to end in their own CI job
(`make test-ci SYSTEM=jenkins-kubernetes`): Jenkins with the plugin, whose
cloud has exactly `CLOUD_RULES`, runs every job of both sides as a pod on a
kind cluster, each side in its own namespace from one spec whose runtime
names none, and each build's pod must hold everything its Jenkinsfile
declares; each side's engine pods must start in that side's namespace. A
namespace without the ConfigMap is refused `build`, `promote` and `lock`
while Jenkins' global environment sets `DATADOUBLE_BOUNDARY=inside`.

- **The plugin's agent image** must be one the pods can pull; the suite
  loads `jenkins/inbound-agent` into the cluster and names it to the plugin
  (`PodTemplateStepExecution.defaultImage`).
- **A finished pod's workspace goes with it,** and the step's outputs are
  in the workspace. The step returns them to the Jenkinsfile; to read them
  from outside, as the suite does, keep finished pods (the cloud's pod
  retention) and read `.datadouble-outputs` from the Datadouble container.

## Outcomes

| Outcome | The build | The step returns |
|---|---|---|
| success | succeeds | `outcome`, `generation` for `run` |
| warning | unstable | as above |
| failure | fails | `error`, the error code |

The step sets the build's description to the generation it built. Only the
`lock` job archives build artifacts: its lock and changes, which hold no data
values.

## Notifications

The spec's `notify` section configures notifications for jobs that fail,
warn or recover. Each `datadouble` step names its job (`job: 'build'`), so Datadouble posts the
spec's webhooks itself after the command, with the build's page
(`BUILD_URL`, set when Jenkins knows its own URL) as `run_url`. Add the
webhook's variable as a Secret text credential like the others.

`notify`'s email is Jenkins' own `mail` step (the Mailer plugin, with the
SMTP server set in Manage Jenkins), in the Jenkinsfile's `post` section:
`failure` and `aborted` (a build stopped at its time limit) for failures,
`unstable` for warnings, and `fixed` for recoveries. Its subject names the
build's result (failure, aborted or unstable, or "recovered" for `fixed`),
and its body adds the build's URL when Jenkins knows it. Jenkins judges
`fixed` from the previous build: a success after a failed or unstable one,
not after an aborted one. Because
`post` runs however the build ended, it also reports a build that failed
before Datadouble started, such as an image that cannot be pulled, which
posts no webhook. A notification that cannot be sent, by mail or webhook, is
logged and leaves the build's result alone.

For a recovery webhook, each command line records its last outcome in a
file `ci init` names (`--ci-record`), under `.datadouble-notify/` in the work
directory on the production side and in staging on the other. Every
production Jenkinsfile then mounts the work directory, so it must exist on
the agent, owned by the engine's user, as for `build`.
