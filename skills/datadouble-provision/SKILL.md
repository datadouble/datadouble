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
