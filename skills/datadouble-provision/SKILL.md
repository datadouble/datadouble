---
name: datadouble-provision
description: Set up how Datadouble makes copies of the staged image for developers and tests - a pool kept ready, or copies made on demand per pull or merge request - and where they are announced. Use when someone wants database copies for CI jobs, review environments or developers.
---

# The provision job

Follow `../RULES.md` throughout. `provision` runs outside the production
boundary.

## Pool or on demand

The spec's `replicate.mode` decides, not `ci init`:

- **pool:** keep `size` unclaimed copies of the active generation ready;
  consumers take one with `datadouble claim --owner NAME`. Suits large
  images.
- **on-demand:** make up to `size` copies for each request (decision 101).
  Suits small images and CI test jobs.

`replicate.using` says what a copy is (a directory, a btrfs subvolume, a
Docker volume or container, a Kubernetes PersistentVolume), and `announce`
where consumers learn about it. Only `dotenv`, `k8s-secret` and `vault`
receive a copy's credentials.

`k8s-pv` and `k8s-secret` reach the cluster the job runs in by its service
account and need nothing more. For jobs, leave `k8s-secret`'s `namespace`
out: its Secrets then go to the namespace the job's pod runs in, so one spec
serves both sides and the service account may already write there. Run from
a laptop, the same spec writes them into the kubeconfig context's namespace
(or `default` when it names none), so name a namespace when people claim
from their own machines and consumers must find every Secret in one place,
or to put the Secrets elsewhere, such as the consumers' namespace, and tell
the person to grant the service account Secrets there. `release` deletes
each Secret from the namespace it was written to, whatever its own
resolves to, so every machine using the spec must reach the same cluster,
and the releasing identity needs `delete` on Secrets in each namespace
copies were announced into. If `release` fails naming announcements left
behind, the copies are already gone: tell the person to delete those
Secrets by hand, and fix the rights. If the spec's Secrets went to `default` before this default
changed, keep `namespace: default` until those copies are released. For a cluster the job runs outside, add
`kubeconfig: {env: NAME}` (and `context` if needed) to each: a secret
reference to the kubeconfig's content, never the runner's own kubeconfig
file, which the job's container cannot see. `{file: /path}` only fits a
secret file mounted for the purpose and readable by the engine's user. The
kubeconfig needs a token or client certificate, since the image has no exec
credential plugins. `ci init` then lists `NAME` with the job's other
secrets; tell the person to store the kubeconfig there (a GitLab variable of
type "Variable", protected, unmasked; see the adapter's README).

## Engine versions and copies

Images are filed by the engine's compatibility line, not its exact version:
`17` for PostgreSQL, `8.4` for MySQL (8.0 and 8.4 are separate lines). A
build follows the source's exact version on its own, so a patch upgrade on the
source needs no spec change. Tell the person that consumers should select
copies by line (`datadouble.dev/engine-version`) and run the newest release of
that line (`mysql:8.4`), which opens every copy of it; Oracle copies need
exactly the version on each copy (`datadouble.dev/server-version`). A Helm chart cannot read the
labels of the volume its claim will bind, since it renders first.

## Logging in to copies

A volume copy (a directory, btrfs subvolume, Docker volume or Kubernetes
PersistentVolume) is a data directory the consumer serves itself, usually
with the engine's official image, which sets passwords only on a new data
directory. Such a copy has only local accounts unless the spec's
`package.accounts` gave its physical image some (see `datadouble-spec`). An
instance copy (a server Datadouble runs on a Docker volume) gets a generated
password, and has those accounts too.

- Only physical images carry accounts, and a build that makes only logical
  images refuses them. The accounts are fixed at build time: changing them
  means a new build, not a new copy.
- Their passwords may be plain text in the spec, since copies hold no
  personal data, or references the build job resolves.
- On PostgreSQL an instance copy's start sets its generated password on
  `postgres`, so a declared `postgres` account's password holds on volume
  copies only.
- `read` accounts get `SELECT` on the data, plus what PostgreSQL's `PUBLIC`
  allows. An `all` account on a copy others can reach can run commands in the
  consumer's container (PostgreSQL's `COPY ... TO PROGRAM`), so say so when
  someone exposes such a copy.
- PostgreSQL copies trust local and loopback logins, as `initdb` set them up:
  anything inside the consumer's container, a sidecar sharing its network, or
  a `kubectl port-forward` logs in as `postgres` without a password. Say so
  when someone plans to rely on the accounts' passwords for protection.

## How each system starts on-demand copies

- GitHub Actions: when a pull request opens; `release` when it closes.
- GitLab CI: in a merge request's pipeline, inside its environment, whose
  stop job releases them when the request is merged or closed. Merge request
  pipelines must be enabled.
- Jenkins: the request's own pipeline starts the job with an `OWNER`
  parameter.
- Argo CD: an ApplicationSet makes an Application per pull request with
  `owner` set; its hooks make and release the copies.

Generate it with the other non-production jobs (`datadouble-fetch`), and pass
on the response's `instructions`.
