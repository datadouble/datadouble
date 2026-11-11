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

Both use `kubectl`, so `KUBECONFIG` and contexts work as usual.
