# Example repository

[datadouble/example-pipeline](https://github.com/datadouble/example-pipeline)
is a complete pipeline for a small shop database: its spec, the GitHub
Actions workflows `datadouble ci init` wrote for both sides of the production
boundary, and a way to run them all on one machine from a fresh clone.

```
git clone https://github.com/datadouble/example-pipeline
cd example-pipeline
make demo
```

`make demo` starts the source, builds the Datadouble image, and runs every
workflow with `act`, on the machine as a self-hosted runner would: build and
promote inside the boundary, then fetch, an on-demand copy for a pull
request, and its release outside it. Its README walks through each file.
