# datadouble-btrfs

The btrfs replicate adapter for [Datadouble](https://datadouble.tightlinesoftware.com/):
each copy is a writable snapshot of the staged image, made in moments however
large the image is.

```yaml
replicate:
  using:
    btrfs:
      path: /srv/clones        # a directory on the same btrfs filesystem as staging
```

Staging must be on btrfs too. The first copy of a generation turns its staged
image into a subvolume; later copies snapshot it. Uses the `btrfs` command.
