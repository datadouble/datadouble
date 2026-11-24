# External services

Datadouble's CI depends on several outside services: GitHub itself, PyPI,
Snyk, SonarQube Cloud, Codecov, GitLab.com and, later, a Salesforce org. Each
needs a one-time setup by a maintainer. This page is the checklist for that
setup.

Each service has its own section, with its steps in the order you do them.
Each step says why the setting matters, so you can judge what to do when a
service's options change.

Two things hold throughout:

- **No secret values go in this repository.** When a step produces a token or
  key, it says which GitHub secret to save it as.
- **Menu names drift.** Service UIs change, so the menu paths below may not
  match exactly. Each step states its goal, so you can find the setting
  anyway.

## The secrets CI uses

All repository secrets go in **datadouble/datadouble → Settings → Secrets and
variables → Actions**. This table lists every one, and links to the section
that creates it.

| Secret | Used for | Set up in |
|---|---|---|
| `SNYK_TOKEN` | Dependency and container scanning | [Snyk](#snyk) |
| `SONAR_TOKEN` | Static analysis | [SonarQube Cloud](#sonarqube-cloud) |
| `GITLAB_MIRROR_SSH_KEY` | Pushing this repository to its GitLab mirror | [GitLab](#gitlabcom) |
| `GITLAB_RUNNER_TOKEN` | Registering the ephemeral runner for nightly GitLab tests | [GitLab](#gitlabcom) |
| `GITLAB_TRIGGER_TOKEN` | Starting the nightly GitLab pipeline | [GitLab](#gitlabcom) |
| `RUNNER_ADMIN_TOKEN` | Registering the nightly tests' ephemeral self-hosted GitHub runner, and starting the workflows it runs | [GitHub](#runner-registration-token) |
| `DATADOUBLE_NIGHTLY_DSN` | The nightly run's throwaway source database. The nightly job starts the database with this DSN's password, so any random password will do | `gh secret set` |
| `SF_CLIENT_ID`, `SF_JWT_KEY`, `SF_USERNAME` | Re-recording the Salesforce cassette on a schedule (not wired up yet) | [Salesforce](#salesforce) |
| `CODECOV_TOKEN` (optional) | Uploading coverage when OpenID Connect can't be used | [Codecov](#codecov) |

Codecov, GitHub Container Registry and PyPI use OpenID Connect, so they need
no secret.

No workflow uses the GitLab or Salesforce secrets yet. Set them up when the
nightly GitLab run and the scheduled re-recording are wired up.

## GitHub

Start with GitHub. The other services sign in with GitHub, and several of the
later steps need the repository's settings in place.

### Organization

Open **datadouble → Settings** and set:

1. **Actions → General:**
    - **Allow actions:** all actions, but require them to be pinned to a
      full-length commit SHA, if the setting is offered. A pinned SHA can't
      be moved to different code the way a tag can.
    - **Workflow permissions:** read repository contents only. Each workflow
      asks for any further permission it needs, job by job.
    - Leave **"Allow GitHub Actions to create and approve pull requests"**
      off, so a workflow can't approve its own changes.
2. **Packages:** allow public packages. Datadouble's container images are
   public.

### Repository

Open **datadouble/datadouble → Settings** and set:

1. **General:** make the repository public.
2. **Code security:** enable these, which find vulnerable dependencies and
   leaked secrets, and give security researchers a private way to report a
   problem:
    - Dependabot alerts;
    - Dependabot security updates;
    - secret scanning, with push protection;
    - private vulnerability reporting.

    CodeQL runs from a workflow, so leave its default setup off. Otherwise it
    would run twice.

3. **Rules → Rulesets:** create a ruleset for `main` that:
    - requires a pull request before merging. During the version 1 run,
      require no approvals, so the run can merge its own pull requests once
      checks pass. Turn on required approvals after version 1.
    - requires status checks to pass. Add the check names once the M0 pull
      request has created them, since GitHub only offers checks it has seen.
    - blocks force pushes and deletion;
    - requires linear history.
4. **Environments:** create an environment named `pypi`, with yourself as a
   required reviewer. Every PyPI release then waits for your approval.
5. **Pages → Build and deployment:**
    - source "GitHub Actions";
    - custom domain `datadouble.tightlinesoftware.com`;
    - HTTPS enforced, once GitHub has issued the certificate.

### Site domain

The documentation site is served at `https://datadouble.tightlinesoftware.com/`.
For that name to reach GitHub Pages, and to stay yours:

1. **Public DNS (Cloudflare):** add a `CNAME` from `datadouble` to
   `datadouble.github.io`. Set it to DNS only, not proxied: GitHub cannot
   issue the site's certificate through Cloudflare's proxy.
2. **Internal DNS:** networks with their own private copy of the
   `tightlinesoftware.com` zone need the same record there. Otherwise the
   name won't resolve inside them.
3. **Domain verification:** verify the domain under **datadouble → Settings →
   Pages → Verified domains**. This stops any other GitHub account from
   serving a site on it.

### Example repository

Create an empty public repository, `datadouble/example-pipeline`. It will
hold the example pipeline for both sides of the production boundary.

### Runner registration token

The nightly hosted tests need a personal access token, saved as
`RUNNER_ADMIN_TOKEN`. It does two jobs:

- **Registering a runner.** The tests register an ephemeral self-hosted
  runner, which needs a token that can create runner registration tokens.
- **Starting the generated workflows.** The job's own token could start build
  and fetch, but not what follows them. GitHub starts no `workflow_run`
  follow-on, such as promote after build, for a run that the job's own token
  started.

To create it:

1. Open **Your settings → Developer settings → Fine-grained personal access
   tokens → Generate new token.**
2. Set:
    - resource owner `datadouble`;
    - repository access only to `datadouble/datadouble`;
    - permissions **Administration: read and write** and **Actions: read and
      write**;
    - expiry one year.
3. Save it as `RUNNER_ADMIN_TOKEN`, and note the expiry date.

A GitHub App with the same permissions is the stronger alternative, and can
replace the token later.

### Container images

Workflows push images to GitHub Container Registry with their own token, so
there is nothing to create first. After the first image is pushed:

1. Open **datadouble → Packages → (package) → Package settings**.
2. Set visibility to public.
3. Check that the package is linked to `datadouble/datadouble`.

## PyPI

Datadouble uses **trusted publishing**: GitHub Actions authenticates to PyPI
with OpenID Connect, so there is no PyPI token anywhere. Instead, you tell
PyPI in advance which workflow may publish each package.

1. **Account.** Create a PyPI account, or use yours, with two-factor
   authentication on.
2. **Pending publishers.** Open **Your account → Publishing → Add a new
   pending publisher**, and add one for each package name in the list below,
   with:
    - **PyPI project name:** the package name;
    - **Owner:** `datadouble`;
    - **Repository name:** `datadouble`;
    - **Workflow name:** `release.yml`;
    - **Environment name:** `pypi`, the environment that waits for your
      approval.
3. **First release.** A pending publisher does not reserve the name. The
   first release from `release.yml` creates the project and turns the pending
   publisher into a normal one.

`release.yml` runs `uv build --all-packages`, so every workspace member except
the workspace itself is a package. These are the package names:

```
datadouble               datadouble-conformance
datadouble-postgres      datadouble-mysql         datadouble-mssql
datadouble-oracle        datadouble-mongodb       datadouble-redis
datadouble-elasticsearch datadouble-opensearch    datadouble-files
datadouble-salesforce    datadouble-s3            datadouble-http
datadouble-kubernetes    datadouble-docker        datadouble-btrfs
datadouble-vault         datadouble-github-actions
datadouble-gitlab        datadouble-jenkins       datadouble-argocd
```

The version 1 run does not tag releases. If it adds package names, its pull
request lists them, and they need pending publishers before the first
release.

## Snyk

Snyk scans the dependencies and the container image. Its tokens belong to a
user, not an organization, and CI scans run under that user's default
organization. So there is no Snyk organization to create.

1. Sign in at `app.snyk.io` with GitHub.
2. Open your personal **account settings** and create a personal access token
   with an expiry date. Note the expiry.
3. Save it as `SNYK_TOKEN`.

CI runs Snyk itself, so the Snyk GitHub integration is optional.

### Sharing one token across repositories

Snyk allows three personal access tokens per user. A maintainer who already
uses all three for other repositories can share one token across them:

1. Set the same value in each repository's `SNYK_TOKEN`.
2. Confirm that each repository's Snyk workflow still authenticates.
   Re-running its latest run uses the new value.
3. Revoke the old tokens.

Rotating the shared token later is the same loop. This script sets the new
value in each repository without echoing it:

```bash
read -rs -p "Snyk token: " T; echo
for r in datadouble/datadouble OTHER/REPO; do
  printf %s "$T" | gh secret set SNYK_TOKEN -R "$r" && echo "set: $r"
done
unset T
```

A red Snyk check can mean either findings or a rejected token, so read the
log before assuming which.

## SonarQube Cloud

SonarQube Cloud runs static analysis and holds the quality gate that every
pull request must pass. It is at `sonarcloud.io`.

1. Sign in with GitHub.
2. Import the `datadouble` GitHub organization. Install the SonarQube Cloud
   app for the `datadouble/datadouble` repository only, and choose the free
   plan.
3. Analyze `datadouble/datadouble`, with the keys CI expects:
    - organization `datadouble`;
    - project `datadouble_datadouble`.
4. Choose GitHub Actions as the analysis method. Then, under the project's
   **Administration → Analysis Method**, turn automatic analysis off. CI
   sends the combined coverage of every test group, which automatic analysis
   would not have.
5. Save the token it generates as `SONAR_TOKEN`.

## Codecov

Codecov shows coverage reports. It only reports: CI's own coverage gate
decides whether a pull request passes.

1. Sign in at `app.codecov.io` with GitHub.
2. Install the Codecov app on the `datadouble` organization, for
   `datadouble/datadouble`.

CI uploads with OpenID Connect (`use_oidc: true`), so no token is needed. If
uploads fail, copy the repository upload token from Codecov's repository
settings into a `CODECOV_TOKEN` secret, and tell whoever maintains CI. CI
uses that token, when it is set, for runs that OpenID Connect can't serve.

## GitLab.com

GitLab hosts two things: the CI/CD Catalog components, and the nightly GitLab
end-to-end tests. Its project is a mirror of this repository, and nobody
commits to it directly.

1. **Group and project.** Create a public group named `datadouble`. If that
   name is taken, pick another and update [CI/CD integration](ci-cd.md).
   Then create an empty public project named `datadouble` in it, with a
   description.
2. **Mirror key.** GitHub Actions pushes to GitLab over SSH, so it needs a
   key that GitLab accepts:
    1. Generate a key pair locally:
       `ssh-keygen -t ed25519 -N '' -C datadouble-mirror -f mirror_key`.
    2. In the GitLab project, open **Settings → Repository → Deploy keys**.
       Add `mirror_key.pub`, with write access granted.
    3. Open **Settings → Repository → Protected branches** and **Protected
       tags**. Allow the deploy key to push to `main` and to `v*` tags.
    4. Save the contents of `mirror_key` as `GITLAB_MIRROR_SSH_KEY`. Then
       delete both key files.
3. **Ephemeral runner.** The nightly tests start their own runner. Open
   **Settings → CI/CD → Runners → New project runner**, with tag
   `datadouble-e2e` and "run untagged jobs" off, so the runner takes only
   these tests' jobs. Save the runner authentication token (it starts with
   `glrt-`) as `GITLAB_RUNNER_TOKEN`.
4. **Trigger token.** This lets GitHub start the nightly pipeline. Open
   **Settings → CI/CD → Pipeline trigger tokens → Add new token**, and save
   it as `GITLAB_TRIGGER_TOKEN`.
5. **CI/CD Catalog.** Wait for the first mirror push, after which the project
   has a README. Then open **Settings → General → Visibility, project
   features, permissions**, and turn on "CI/CD Catalog project".

## Salesforce

The Salesforce adapter's tests replay a cassette and run against a fake org,
so CI needs no org (decision 111). These steps are only for a scheduled
re-recording of the cassette, which catches API changes and cassette drift.
Until that is wired up, a maintainer re-records by hand with `make
record-salesforce ORG=<alias>` (see [local
development](local-development.md#salesforce)).

Use a Developer Edition or scratch org holding only made-up records, never a
production org.

1. **Org.** Sign up for a free Developer Edition org. Create a dedicated user
   for recording, with a profile or permission set that:
    - can read the recorded objects;
    - has "View Setup and Configuration", which the recorder needs to see the
      installed packages.
2. **Certificate.** The CI job will log in with the JWT bearer flow, which
   needs no password. Generate a key pair and a self-signed certificate
   locally:

    ```bash
    openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj '/CN=datadouble-recorder' -keyout recorder.key -out recorder.crt
    ```

3. **Connected app.** In Setup, create an external client app (or a connected
   app) named `datadouble-recorder`, with:
    - OAuth enabled;
    - the callback URL `http://localhost:1717/OAuthRedirect`;
    - the scopes "Manage user data via APIs (api)" and "Perform requests at
      any time (refresh_token, offline_access)";
    - "Use digital signatures", with `recorder.crt`;
    - the JWT bearer flow allowed.

    Under its policies, set permitted users to "Admin approved users are
    pre-authorized", and add the recording user's profile or permission set.
    This lets the job log in without anyone approving it interactively.

4. **Secrets.** Save these with `gh secret set`, so the values are never
   echoed:
    - the app's consumer key as `SF_CLIENT_ID`;
    - the contents of `recorder.key` as `SF_JWT_KEY`;
    - the recording user's username as `SF_USERNAME`.

    Then delete both files.

Once it is wired up, the job will log in with:

```bash
sf org login jwt --client-id "$SF_CLIENT_ID" --jwt-key-file <file> --username "$SF_USERNAME" --alias recorder
```

It will then run `make record-salesforce ORG=recorder`, and open a pull
request if the cassette changed.
