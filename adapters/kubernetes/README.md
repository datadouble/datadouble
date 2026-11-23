# datadouble-kubernetes

Kubernetes adapters for [Datadouble](https://datadouble.tightlinesoftware.com/):

- **`k8s-pv`** (replicate): each copy is a directory and a PersistentVolume
  pointing at it, labelled so consumers select it in a PersistentVolumeClaim.
  A bound volume counts as claimed; once its claim is deleted the copy is
  removed and the pool refilled.
- **`k8s-secret`** (announce): a Secret per copy holding where it is and, for
  instance copies, its credentials.
- **`kubernetes`** (container runtime): engine software in pods, sharing the
  work directory through a PersistentVolumeClaim (decision 73).

```yaml
replicate:
  using:
    k8s-pv:
      path: /srv/clones              # where copies are written
      local:                         # a local volume on that node, or
        node: worker-1
      # nfs:
      #   server: nfs.internal
      #   path: /exports/clones
      storage_class: datadouble
announce:
  - k8s-secret:
      namespace: dev
```

Each volume carries `datadouble.dev/` labels: `spec`, `group`, `engine`,
`engine-version`, `server-version`, `generation` and `copy`. `engine-version`
is the engine's compatibility line (`17` for PostgreSQL, `8.4` for MySQL), not
the exact version: select on it, and serve copies with the newest release of
that line (`postgres:17`, `mysql:8.4`), which opens every copy of the line
(Oracle copies need exactly their `server-version`). `server-version` is the
exact version that wrote the copy, for consumers that must match it; for
engines without a server (Salesforce, file trees) it is the image layout's
version. A Helm chart renders its image before any claim
binds, so it cannot read the bound volume's labels; choose the image from the
line, or look up an available volume's `server-version` before rendering.

All three drive `kubectl` (`DATADOUBLE_KUBECTL` names another binary).

## The runtime's namespace

```yaml
runtime:
  kubernetes:
    claim: datadouble          # the PersistentVolumeClaim holding the work directory
    mount: /srv/datadouble     # where Datadouble's own pod mounts it
    # namespace: shop-staging  # optional
```

Leave `namespace` out unless pods must start somewhere else. Then they start
where `kubectl` would put them unasked: inside a cluster (no `kubeconfig` in
the settings, no `KUBECONFIG`, and a service account namespace file in
Datadouble's pod), that pod's own namespace, read from
`/var/run/secrets/kubernetes.io/serviceaccount/namespace`; otherwise the
namespace of the kubeconfig's context (the settings' `context`, or its current
one), or `default` when it names none. So one spec serves CI/CD jobs that run
as pods in each side's own namespace, which `ci init` requires a set
namespace to match (decision 133).

## Reaching the cluster

Without settings, `kubectl` uses the pod's service account when Datadouble
runs in the cluster, or the ambient configuration (`KUBECONFIG`,
`~/.kube/config`) otherwise. A job in the cluster it drives, such as the Argo
CD jobs, needs nothing more.

To drive a cluster from outside it, give each adapter that talks to it a
`kubeconfig`, a secret reference to a kubeconfig's content, and optionally
the `context` to use:

```yaml
runtime:
  kubernetes:
    claim: datadouble-work
    mount: /work
    kubeconfig:
      env: BUILD_KUBECONFIG
    context: build
replicate:
  using:
    k8s-pv:
      path: /srv/clones
      local:
        node: worker-1
      kubeconfig:
        env: DEV_KUBECONFIG
announce:
  - k8s-secret:
      namespace: dev
      kubeconfig:
        env: DEV_KUBECONFIG
```

- **A secret reference to the content, not the runner's kubeconfig file.**
  `{env: NAME}` holds the kubeconfig itself, not a path to one: in CI jobs
  Datadouble runs in its image, which sees only the paths the spec names,
  and as the engine's user (decision 86), who cannot read the runner user's
  `~/.kube/config`. `{file: /path}` is for a secret file mounted for this
  purpose, such as a Kubernetes Secret volume: `ci init` mounts that path
  into the job, so it must exist on the runner and be readable by the
  engine's user (`999:999` for PostgreSQL). On Argo CD it must also be under
  the claim's directory, so prefer `{env: NAME}` there.
- **Credentials the image can use.** The Datadouble image holds `kubectl`
  and nothing else, so a kubeconfig whose user runs an exec credential
  plugin (`aws eks get-token`, `gke-gcloud-auth-plugin`, `kubelogin`) fails.
  For EKS, GKE or AKS, use a kubeconfig with a service account token or a
  client certificate, embedded in it (`token`, `client-certificate-data`),
  not in files it names (`tokenFile`, `client-certificate`), since those
  paths are not mounted.
- **`ci init` lists the variable** among the secrets of each job whose part
  of the spec names it, and only those: the runtime's for `build`,
  `provision` and `release`; `k8s-pv`'s for `provision`, `release` and the
  non-production `prune`; `k8s-secret`'s for `provision` and `release`.
  Define it in each system's own secret store, as for any other secret
  (repository secrets, CI/CD variables, Jenkins credentials, a Kubernetes
  Secret, or a Vault-backed secret the CI system injects).
- **Defining a multi-line secret.** GitHub Actions and Kubernetes Secrets
  take a kubeconfig as it is. In GitLab, make it a CI/CD variable of type
  "Variable", not "File" (a File variable holds a path, which the job's
  container cannot see), protected, with masking off: GitLab masks only
  single-line values. In Jenkins, a Secret text credential holds one line in
  the web form, so create a multi-line one through the API or Configuration
  as Code.
- **Datadouble writes the kubeconfig to a private temporary file** for each
  `kubectl` call: a file only its user can read (mode 0600), in a directory
  of its own (mode 0700), passed with `--kubeconfig` (and `--context`), both
  removed when the call ends, even after a failure or when the job is
  cancelled (SIGTERM); a `kubernetes` port-forward keeps its file until the
  forward stops. The directory is made in the system's temporary directory
  (`TMPDIR`), or in the home directory when that is under the `kubernetes`
  runtime's `mount`, which pods see; if both are, the call fails, asking for
  `TMPDIR` elsewhere.
- **What is hidden.** Every string value in the kubeconfig at least 16
  characters long (tokens, keys, certificates, server URLs) is replaced with
  `[redacted]` in everything `kubectl` prints that Datadouble passes on, and
  only those values are handed to the CI system's log masking, so lines such
  as `kind: Config` stay readable. A kubeconfig that cannot be resolved
  fails with `unresolved_reference` before any work, including before a
  `kubernetes` runtime starts a pod.
