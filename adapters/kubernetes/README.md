# datadouble-kubernetes

Kubernetes adapters for [Datadouble](https://datadouble.tightlinesoftware.com/):

- **`k8s-pv`** (replicate): each copy is a directory and a PersistentVolume
  pointing at it, labelled so consumers select it in a PersistentVolumeClaim.
  A bound volume counts as claimed; once its claim is deleted the copy is
  removed and the pool refilled.
- **`k8s-secret`** (announce): a Secret per copy holding where it is and, for
  instance copies, its credentials.

```yaml
replicate:
  using:
    k8s-pv:
      path: /srv/clones              # where copies are written
      local: {node: worker-1}        # a local volume on that node, or
      # nfs: {server: nfs.internal, path: /exports/clones}
      storage_class: datadouble
announce:
  - k8s-secret: {namespace: dev}
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

Both use `kubectl`, so `KUBECONFIG` and contexts work as usual.
