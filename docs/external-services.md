# External services

One-time setup a maintainer does for the services CI depends on. No secret
values belong in this repository; each step says which GitHub secret to create.
Service UIs change, so menu names below may drift; the goal of each step is
stated so it can be found anyway.

All repository secrets go in **datadouble/datadouble → Settings → Secrets and
variables → Actions**.

| Secret | Used for | Set up in |
|---|---|---|
| `SNYK_TOKEN` | Dependency and container scanning | [Snyk](#snyk) |
| `SONAR_TOKEN` | Static analysis | [SonarQube Cloud](#sonarqube-cloud) |
| `GITLAB_MIRROR_SSH_KEY` | Pushing this repository to its GitLab mirror | [GitLab](#gitlabcom) |
| `GITLAB_RUNNER_TOKEN` | Registering the ephemeral runner for nightly GitLab tests | [GitLab](#gitlabcom) |
| `GITLAB_TRIGGER_TOKEN` | Starting the nightly GitLab pipeline | [GitLab](#gitlabcom) |
| `RUNNER_ADMIN_TOKEN` | Registering ephemeral self-hosted GitHub runners for nightly tests | [GitHub](#github) |

Codecov, GitHub Container Registry and PyPI use OpenID Connect and need no
secret.

## GitHub

### Organization

**datadouble → Settings:**

1. **Actions → General:**
   - Allow actions: all actions, but require them to be pinned to a full-length
     commit SHA, if the setting is offered.
   - Workflow permissions: read repository contents only.
   - Leave "Allow GitHub Actions to create and approve pull requests" off.
2. **Packages:** allow public packages.

### Repository

**datadouble/datadouble → Settings:**

1. **General:** the repository is public.
2. **Code security:** enable Dependabot alerts, Dependabot security updates,
   secret scanning with push protection, and private vulnerability reporting.
   CodeQL runs from a workflow, so leave its default setup off.
3. **Rules → Rulesets:** a ruleset for `main` that:
   - requires a pull request before merging, with no required approvals during
     the version 1 run (so the run can merge its own pull requests once checks
     pass); turn on required approvals after version 1
   - requires status checks to pass (add the check names once the M0 pull
     request has created them)
   - blocks force pushes and deletion
   - requires linear history
4. **Environments:** create `pypi` with yourself as a required reviewer, so
   every PyPI release waits for your approval.
5. **Pages → Build and deployment:** source "GitHub Actions". The site is
   served at `https://datadouble.github.io/datadouble/`.

### Example repository

Create an empty public repository, `datadouble/example-pipeline`.

### Runner registration token

The nightly hosted tests register ephemeral self-hosted runners, which needs a
token that can create runner registration tokens.

1. **Your settings → Developer settings → Fine-grained personal access
   tokens → Generate new token.**
2. Resource owner `datadouble`, repository access only `datadouble/datadouble`,
   permission **Administration: read and write**, expiry one year.
3. Save it as `RUNNER_ADMIN_TOKEN`, and note the expiry date.

A GitHub App with the same permission is the stronger alternative and can
replace the token later.

### Container images

Images are pushed to GitHub Container Registry with the workflow's own token.
After the first image is pushed, open **datadouble → Packages → (package) →
Package settings**, set visibility to public, and check it is linked to
`datadouble/datadouble`.

## PyPI

Trusted publishing: GitHub Actions authenticates to PyPI with OpenID Connect,
so there is no PyPI token anywhere.

1. Create a PyPI account (or use yours), with two-factor authentication on.
2. **Your account → Publishing → Add a new pending publisher**, once per
   package name:
   - PyPI project name: see the list below
   - Owner: `datadouble`
   - Repository name: `datadouble`
   - Workflow name: `release.yml`
   - Environment name: `pypi`
3. A pending publisher does not reserve the name. The first release from
   `release.yml` creates the project and turns the pending publisher into a
   normal one.

Package names known now:

```
datadouble
datadouble-postgres      datadouble-mysql         datadouble-mssql
datadouble-oracle        datadouble-mongodb       datadouble-redis
datadouble-elasticsearch datadouble-opensearch    datadouble-files
datadouble-salesforce    datadouble-s3            datadouble-kubernetes
datadouble-btrfs
```

The version 1 run does not tag releases. If it adds package names, its pull
request lists them, and they need pending publishers before the first release.

## Snyk

1. Sign in at `app.snyk.io` with GitHub.
2. Create an organization named `datadouble` (or use your personal one).
3. Copy an API token: an organization service account token where your plan
   offers one, otherwise your personal API token from account settings.
4. Save it as `SNYK_TOKEN`.

CI runs Snyk itself, so the Snyk GitHub integration is optional.

## SonarQube Cloud

Formerly SonarCloud, still at `sonarcloud.io`.

1. Sign in with GitHub.
2. Import the `datadouble` GitHub organization, installing the SonarQube Cloud
   app for the `datadouble/datadouble` repository only. Choose the free plan.
3. Analyze `datadouble/datadouble`. The keys CI expects:
   - organization `datadouble`
   - project `datadouble_datadouble`
4. Choose GitHub Actions as the analysis method, and under the project's
   **Administration → Analysis Method**, turn automatic analysis off.
5. Save the token it generates as `SONAR_TOKEN`.

## Codecov

1. Sign in at `app.codecov.io` with GitHub.
2. Install the Codecov app on the `datadouble` organization for
   `datadouble/datadouble`.

CI uploads with OpenID Connect (`use_oidc: true`), so no token is needed. If
uploads fail, copy the repository upload token from Codecov's repository
settings into a `CODECOV_TOKEN` secret and tell whoever maintains CI.

## GitLab.com

GitLab hosts the CI/CD Catalog components and the nightly GitLab end-to-end
tests. Its project is a mirror of this repository; nobody commits to it
directly.

1. **Group and project.** Create a public group `datadouble` (if the name is
   taken, pick another and update [CI/CD integration](ci-cd.md)), then an empty
   public project `datadouble` in it, with a description.
2. **Mirror key.** GitHub Actions pushes to GitLab over SSH:
   1. Generate a key pair locally: `ssh-keygen -t ed25519 -N '' -C datadouble-mirror -f mirror_key`.
   2. In the GitLab project, **Settings → Repository → Deploy keys**: add
      `mirror_key.pub` with write access granted.
   3. **Settings → Repository → Protected branches and Protected tags:** allow
      the deploy key to push to `main` and to `v*` tags.
   4. Save the contents of `mirror_key` as `GITLAB_MIRROR_SSH_KEY`, then delete
      both files.
3. **Ephemeral runner.** **Settings → CI/CD → Runners → New project runner**,
   with tag `datadouble-e2e` and "run untagged jobs" off. Save the runner
   authentication token (it starts `glrt-`) as `GITLAB_RUNNER_TOKEN`.
4. **Trigger token.** **Settings → CI/CD → Pipeline trigger tokens → Add new
   token.** Save it as `GITLAB_TRIGGER_TOKEN`.
5. **CI/CD Catalog.** After the first mirror push (the project then has a
   README), **Settings → General → Visibility, project features, permissions**:
   turn on "CI/CD Catalog project".
