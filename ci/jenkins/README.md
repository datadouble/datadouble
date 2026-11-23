# datadouble-jenkins

The Jenkins CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It has two parts:

- **The shared library in this directory** (`vars/datadouble.groovy` and
  `resources/datadouble/run.sh`), whose `datadouble` step runs one
  `datadouble` command in the Datadouble image.
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

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and archives the new `catalog.lock` and `changes.txt` as the
build's artifacts. It never commits: download them from the build's page,
check every new or changed column in `changes.txt` for personal data, then
commit `catalog.lock` beside the spec. A lock holds names, types and versions,
never values (decision 123).

## What a job needs on its agent

- The labels given to `--runner`.
- Docker, with its socket at `/var/run/docker.sock` unless the agent says
  otherwise (below), usable by the agent's user. The step runs the
  Datadouble image as the engine's user on the host's network, with the
  workspace and every absolute path the spec names mounted at the same path.
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

The spec's `notify` section says who hears about a job that fails or warns.
Each `datadouble` step names its job (`job: 'build'`), so Datadouble posts the
spec's webhooks itself after the command, with the build's page
(`BUILD_URL`, set when Jenkins knows its own URL) as `run_url`. Add the
webhook's variable as a Secret text credential like the others.

`notify`'s email is Jenkins' own `mail` step (the Mailer plugin, with the
SMTP server set in Manage Jenkins), in the Jenkinsfile's `post` section:
`failure` and `aborted` (a build stopped at its time limit) for failures,
`unstable` for warnings. Its subject names the build's result (failure,
aborted or unstable), and its body adds the build's URL when Jenkins knows
it. Because
`post` runs however the build ended, it also reports a build that failed
before Datadouble started, such as an image that cannot be pulled, which
posts no webhook. A notification that cannot be sent, by mail or webhook, is
logged and leaves the build's result alone.
