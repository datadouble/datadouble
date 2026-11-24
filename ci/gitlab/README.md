# datadouble-gitlab

The GitLab CI/CD adapter for
[Datadouble](https://datadouble.tightlinesoftware.com/). It writes jobs for
`datadouble ci init --system gitlab` and, at run time, turns a response into
a dotenv report and an exit code GitLab understands.

## Writing jobs

```
datadouble ci init --system gitlab --job build,promote,prune,lock \
    --spec datadouble/shop.yml --runner prod-db --work /srv/datadouble/work
datadouble ci init --system gitlab --job fetch,provision,release,prune,activate \
    --spec datadouble/shop.yml --runner staging
```

Each call writes one file, `.gitlab/datadouble-<spec>-<side>.yml`, and lists
what to set up in GitLab:

- **Include the file** from `.gitlab-ci.yml` (`include: local`).
- **Create the pipeline schedules** it names. GitLab keeps schedules outside
  its configuration, so a schedule sets `DATADOUBLE_JOB` to the job it runs
  (`shop-build`, `shop-fetch`). Running a pipeline by hand works the same way.
- **Run merge request pipelines,** if the spec makes on-demand copies: a merge
  request's pipeline makes copies for `mr-<iid>` in its environment, and the
  environment's stop job releases them when the request is merged or closed.
- **Define the variables** the jobs read secrets from as masked, protected
  CI/CD variables. Each job passes on only the ones its part of the spec
  names. Use the "Variable" type, not "File": a File variable holds a path
  the job's container cannot see. GitLab masks only single-line values, so
  a multi-line one such as a kubeconfig is protected with masking off (see
  the Kubernetes adapter's README).

The file includes the job template, `datadouble.yml` in this package, from
this repository at the release's tag, pinned by its SHA-256 (`include:
remote` with `integrity`). GitLab must be able to reach
`raw.githubusercontent.com`; on a GitLab without that, mirror the file and
change the URL. The CI/CD Catalog components follow once the GitLab mirror of
this repository exists.

## Re-locking the catalog

The manual `lock` job (`--job lock`, on the production side with `build`)
re-locks after a schema change. It runs `datadouble lock --output
datadouble-lock` and keeps the new `catalog.lock` and `changes.txt` in the
job's artifacts with `datadouble.env`; start it by running a pipeline with
`DATADOUBLE_JOB` set to `<spec>-lock`. It never commits: download the job's
artifacts, check every new or changed column in `changes.txt` for personal
data, then commit `catalog.lock` beside the spec. A lock contains names, types
and versions, never values (decision 123).

## What a job needs on its runner

- Tags matching `--runner`; `ci init` refuses GitLab.com's own `saas-` tags.
- The shell executor, with Docker and its socket at `/var/run/docker.sock`,
  unless the runner says otherwise (below). The template runs the Datadouble
  image as the engine's user on the host's network, with the project
  directory and every absolute path in the spec mounted at the same path.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL).
- On runners inside the production boundary, `DATADOUBLE_BOUNDARY=inside` in
  the runner's `environment` (decision 98).

### Another Docker command or socket

The job template runs the image with `docker` and the daemon at
`/var/run/docker.sock`. Where Docker is installed outside `PATH`, or the
daemon is another Docker-compatible one listening on another socket, set
`DATADOUBLE_DOCKER` (one program, a name or an absolute path) and
`DATADOUBLE_DOCKER_SOCKET` (an absolute path) in the runner's `environment`,
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
shared at the same paths (decision 73) line up. The job template reads the
socket's group with GNU `stat`, so the runner needs GNU coreutils, as Linux
distributions have.

## Outcomes

| Outcome | The job | Outputs |
|---|---|---|
| success | passes | `DATADOUBLE_OUTCOME`, `DATADOUBLE_GENERATION` for `run` |
| warning | fails, allowed (exit code 3), shown as a warning | as above |
| failure | fails | `DATADOUBLE_ERROR`, the error code |

The outputs are a dotenv report (`datadouble.env`), which later jobs in the
pipeline read as variables. GitLab masks only variables defined as masked, so
Datadouble cannot mask anything else at run time.

## Notifications

The spec's `notify` section configures notifications for jobs that fail or warn.
Each job sets `DATADOUBLE_CI_JOB` to its job, so Datadouble posts the spec's
webhooks itself after the command, with the job's page (`CI_JOB_URL`) as
`run_url`. Define the webhook's variable as a masked CI/CD variable like the
others. On-demand provision and release run in merge request pipelines,
which see no protected variables, so for those jobs the webhook's variable
must not be protected, or must be scoped to the environments they use;
`ci init` says so in `instructions`. A webhook that cannot be reached is a warning line in the log; the
job's result is unchanged.

GitLab CI has no mail step. For `notify`'s email, `ci init` lists the setup
in `instructions`: the project's Pipeline status emails integration
(Settings > Integrations), for those addresses, notifying only broken
pipelines. It mails failed pipelines only, so a warning, a job allowed to
fail, mails nobody; list a webhook to hear about warnings. Those emails also
report a job that fails before Datadouble starts, such as an image that
cannot be pulled, which posts no webhook.
