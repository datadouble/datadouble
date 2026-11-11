# datadouble-docker

The Docker replicate adapter for [Datadouble](https://datadouble.tightlinesoftware.com/):
copies of staged images as Docker volumes, or as containers running a server on
such a volume.

```yaml
replicate:
  using:
    docker-volume:
      kind: instance            # or volume (the default)
      server_image: postgres:18-trixie@sha256:...   # optional; the engine's choice otherwise
      host: db-clones.internal  # how consumers reach published ports
      network: ci               # optional: attach instances to a Docker network
```

It uses the `docker` command, so `DOCKER_HOST` and Docker contexts work as usual.
