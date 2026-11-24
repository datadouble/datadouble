# Example repository

The quickstart builds one image by hand. A real pipeline does more: it builds
and promotes images inside the production boundary, then fetches them and
makes copies outside it, all from CI jobs. The example repository shows that
whole pipeline working, so you can see how the pieces fit before you set up
your own.

## What's in it

[datadouble/example-pipeline](https://github.com/datadouble/example-pipeline)
is a complete pipeline for a small shop database. It holds:

- the spec and its catalog lock;
- the GitHub Actions workflows that `datadouble ci init` wrote, for both sides
  of the production boundary;
- a way to run them all on one machine from a fresh clone.

## Running it

```
git clone https://github.com/datadouble/example-pipeline
cd example-pipeline
make demo
```

You need Docker, git and `act`.

`make demo` builds the Datadouble image from a checkout of
datadouble/datadouble, starts a throwaway source, and makes a signing key
pair. Then it runs every workflow with `act`, on your machine, the way a
self-hosted runner would. The workflows run in pipeline order:

1. Inside the boundary: build an image, then promote it.
2. Outside the boundary: fetch the image, make an on-demand copy for a pull
   request, then release that copy.
3. On both sides: prune old images.

Everything the demo makes goes under `/var/tmp/example-pipeline`, and the
demo removes it at the end.

The repository's README walks through each file.
