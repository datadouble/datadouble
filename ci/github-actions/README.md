# datadouble-github-actions

The GitHub Actions CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It has two parts:

- **The action in this directory,** `datadouble/datadouble/ci/github-actions`,
  which runs one `datadouble` command in the Datadouble image.
- **The Python package,** which writes workflows for `datadouble ci init
  --system github-actions` and, at run time, turns a response into step
  outputs, a job summary, annotations and log masks.

## Writing workflows

```
datadouble ci init --system github-actions --job build,promote,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system github-actions --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

Each job gets its own workflow, `.github/workflows/datadouble-<spec>-<job>.yml`,
with its schedule (in UTC), its trigger by another job (`promote` after a
successful `build`, a pool's `provision` after `fetch`), pull request triggers
for on-demand copies, and a concurrency group. The response lists the
environment variables the jobs read secrets from; define each as a
repository or environment secret. Each job receives only the ones its part of
the spec references. Name credentials an adapter reads from the environment rather
than the spec, such as `AWS_ACCESS_KEY_ID`, with `--env NAME`.

`build`, `promote` and `lock` need `--runner`, a self-hosted runner's label
inside the production boundary, and `build` needs `--work`, a directory in
production scratch space that outlives the job, so a retried run resumes its
generation. `ci init` refuses GitHub's own runner labels for every job: the
jobs need runners that keep their files, and build, promote and lock need
runners inside the boundary.

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and uploads the new lock (named after the spec, such as
`shop.lock`) and `changes.txt` as the workflow run's artifact,
`datadouble-<spec>-lock`, with `actions/upload-artifact`. It never commits:
download the artifact from the run's page, check every new or changed column
in `changes.txt` for personal data, then commit the lock beside the spec. A
lock contains names, types and versions, never values (decision 123).

## What a job needs on its runner

- The mark `DATADOUBLE_BOUNDARY=inside` in the runner's `.env`, on runners
  inside the production boundary (decision 98).
- Docker, with its socket at `/var/run/docker.sock` unless the runner says
  otherwise (below). The action runs the Datadouble image on the host's
  network, with the workspace and every absolute path in the spec mounted
  at the same path, so the engine containers Datadouble starts can mount
  them too.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL), which Datadouble runs as. The action fails on a path the
  runner lacks, rather than let Docker create it owned by root.

### Another Docker command or socket

The action runs the image with `docker` and the daemon at
`/var/run/docker.sock`. Where Docker is installed outside `PATH`, or the
daemon is another Docker-compatible one listening on another socket, set
`DATADOUBLE_DOCKER` (one program, a name or an absolute path) and
`DATADOUBLE_DOCKER_SOCKET` (an absolute path) in the runner's `.env` file,
once for every job on the runner. Or pass `--docker` and `--docker-socket`
to `ci init`, which writes them into the generated configuration, where they
take precedence over the runner's own (decision 126).

Without `DATADOUBLE_DOCKER_SOCKET`, a `DOCKER_HOST` of the form
`unix://PATH` in the runner's environment names the socket. A socket named
either way is also the daemon the image is started on (`DOCKER_HOST`, and
`CONTAINER_HOST` for podman), so the image and the engines' containers share
one daemon. The socket is mounted at `/var/run/docker.sock` inside the
Datadouble container, and `DOCKER_HOST` there always points at it, even when
a job passes its own `DOCKER_HOST` on.

Rootless Docker is not supported: its user namespace maps the engine's user
to another one on the host, so neither the socket's group nor the files
shared at the same paths (decision 73) line up. The action reads the
socket's group with GNU `stat`, so the runner needs GNU coreutils, as Linux
distributions have.

## Outcomes

| Outcome | The step | Annotation |
|---|---|---|
| success | passes | none |
| warning | passes | a warning per warning |
| failure | fails | an error with the reason |

Step outputs are `outcome`, `generation` (for `run`) and `error` (the error
code, for a failure), and `fingerprint` and `changes` (their number) for
`lock`. The action declares each, so a workflow step with an `id` reads them
as `steps.<id>.outputs.<name>`. Datadouble never writes data artifacts to GitHub's artifact storage;
the `lock` job's lock and changes hold no data values.

## Notifications

The spec's `notify` section configures notifications for jobs that fail,
warn or recover. Each step passes the action its job (`job: build`), so Datadouble posts the
spec's webhooks itself after the command, with the run's page
(`$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID`) as
`run_url`. The webhook's variable is a secret like the others.

A workflow cannot send mail. GitHub mails a failed run only to whoever
started it (for a schedule, whoever last changed the workflow), under their
own Actions notification settings, and never a warning, which is a passing
run, nor a recovery, which GitHub doesn't tell from any other passing run. For `notify`'s email, `ci init` says so in `instructions`; list a
webhook to a service that mails the team instead. GitHub's own failed-run
mail is also what reports a job that fails before Datadouble starts, such as
an image that cannot be pulled, since no webhook is posted then. A webhook
that cannot be reached is a warning line in the log; the step's result is
unchanged.
