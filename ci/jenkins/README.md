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
datadouble ci init --system jenkins --job build,promote,prune \
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
  Jenkins masks them in logs.

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

## What a job needs on its agent

- The labels given to `--runner`.
- Docker, with its socket at `/var/run/docker.sock`, usable by the agent's
  user. The step runs the Datadouble image as the engine's user on the host's
  network, with the workspace and every absolute path the spec names mounted
  at the same path.
- Those paths, existing and owned by the engine's user (`999:999` for
  PostgreSQL).
- On agents inside the production boundary, `DATADOUBLE_BOUNDARY=inside` in
  the agent's environment (decision 98).

## Outcomes

| Outcome | The build | The step returns |
|---|---|---|
| success | succeeds | `outcome`, `generation` for `run` |
| warning | unstable | as above |
| failure | fails | `error`, the error code |

The step sets the build's description to the generation it built. Nothing is
archived as a build artifact.
