# Using Datadouble

This section is about running Datadouble and wiring it into a team's
pipelines. The design behind it, and the reasons for it, are in the Design
section.

If you are new, start with the quickstart. It builds a golden image on your
own machine and introduces the ideas the other pages assume.

## Getting started

- [Quickstart](quickstart.md): a golden image from a small PostgreSQL
  database on your own machine, in a few minutes.
- [Example repository](example-repository.md): a complete pipeline, both
  sides of the production boundary, to clone and run.

## Setting up a pipeline

- [Spec sizes](spec-sizes.md): a micro, a QA and a staging copy from one
  spec, with `extends`, and the lock each size uses.
- [Extensions, plugins and modules](source-requirements.md): sources that
  use more than the bare server, and the images their copies need.
- [CI/CD libraries](ci-cd-libraries.md): jobs for GitHub Actions, GitLab
  CI, Jenkins and Argo CD, written by `datadouble ci init`.
- [Agent skills](agent-skills.md): the Claude Code plugin that sets a
  pipeline up for you.

## Reference

These pages are generated from the code, so they always match it.

- [Spec reference](spec-reference.md): all spec configuration options,
  generated from the JSON Schema.
- [CLI reference](cli-reference.md): every command and option, generated
  from the CLI.
