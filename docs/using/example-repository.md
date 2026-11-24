# Example repository

The quickstart builds one image by hand. A real pipeline does more: it builds
and promotes images inside the production boundary, then fetches them and
makes copies outside it, all from CI jobs. The example repository shows that
whole pipeline working, so you can see how the pieces fit before you set up
your own.

## What's in it

[datadouble/example-pipeline](https://github.com/datadouble/example-pipeline)
is a complete pipeline for a small shop database. It holds:

- the spec;
- the GitHub Actions workflows that `datadouble ci init` wrote, for both sides
  of the production boundary;
- a way to run them all on one machine from a fresh clone.

## Running it

```
git clone https://github.com/datadouble/example-pipeline
cd example-pipeline
make demo
```

`make demo` starts the source and builds the Datadouble image. Then it runs
every workflow with `act`, on your machine, the way a self-hosted runner
would. The workflows run in pipeline order:

1. Inside the boundary: build an image, then promote it.
2. Outside the boundary: fetch the image, make an on-demand copy for a pull
   request, then release that copy.

The repository's README walks through each file.
