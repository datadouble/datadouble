---
name: datadouble-activate
description: Roll Datadouble's staging back to an earlier golden image, so new copies come from it, and set up the manual job that does it. Use when a new image turns out to be bad, or someone asks how to roll back.
---

# The activate job

Follow `../RULES.md` throughout. `activate` runs outside the production
boundary, by hand.

## Rolling back

It points the staging channel at a generation still in staging (`fetch`
keeps the last `keep`); new copies then come from it, and pool copies of
other generations are replaced as `provision` runs. A later `fetch` does not
undo it: only a newer promoted image moves the channel on (decision 59).

- GitHub Actions: run the workflow with the `generation` input.
- GitLab CI: run a pipeline with `DATADOUBLE_JOB` and `DATADOUBLE_GENERATION`.
- Jenkins: build the job with the `GENERATION` parameter.
- Argo CD: set `generation` in the Application's values, then create a job
  from its CronJob.

Generate it with the other non-production jobs (`datadouble-fetch`).
