# datadouble-vault

The Vault announce adapter for [Datadouble](https://datadouble.tightlinesoftware.com/):
one KV version 2 secret per copy, holding where the copy is and its
credentials. It works with HashiCorp Vault and OpenBao.

```yaml
announce:
  - vault:
      address: https://vault.internal.example:8200
      mount: secret          # the KV v2 mount (default: secret)
      path: datadouble/clones  # entries go under <path>/<copy id>
```

The token comes from `VAULT_TOKEN`; it never appears in the spec.

`mount` and `path` are inside Vault, not on the job's machine, so `ci init`
never mounts them into a job, even when they start with `/`
([which settings become mounts](../../docs/ci-cd.md#which-settings-become-mounts)).
