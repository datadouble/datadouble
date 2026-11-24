# Agent skills

You can have a coding agent set up Datadouble for you. Datadouble ships a
Claude Code plugin whose skills set up a pipeline, or any part of one, in a
team's own repositories. This page shows how to install it, what to ask for,
and what the skills will and won't do. The design is in
[agent skills](../agent-skills.md).

## Install

This repository is the plugin's marketplace. Add it, then install the
plugin from it:

```
/plugin marketplace add datadouble/datadouble
/plugin install datadouble@datadouble
```

## Ask for what you need

Then ask in plain words. For example:

- "set up the Datadouble pipeline for datadouble/shop.yml in our GitLab CI"
- "copy the avatars from our uploads too"
- "roll staging back to yesterday's image"

The agent picks the skill that fits:

| Skill | Sets up |
|---|---|
| `datadouble-pipeline` | Everything, in order, with one summary at the end |
| `datadouble-spec` | The spec, checked with `datadouble plan`, and re-locking through the `lock` job with the changes reviewed for personal data |
| `datadouble-files` | File trees, with each `find` expression explained and tried first |
| `datadouble-build`, `datadouble-promote` | The production jobs, `lock` among them |
| `datadouble-fetch`, `datadouble-provision`, `datadouble-release`, `datadouble-activate` | The non-production jobs |
| `datadouble-prune` | Retention, with a `prune` job on each side of the boundary |

## What the skills won't do

Every skill follows the same rules, in `skills/RULES.md`:

- CI/CD configuration comes only from `datadouble ci init`;
- safety settings are never weakened to make a run pass;
- production jobs never go on a vendor-hosted runner;
- the skills never handle secrets or connect to production.

Asked to break one of these, the agent explains why it won't, and what to do
instead.

## Versions and other agents

The skills are released with Datadouble. Each one checks that `datadouble
version` matches the plugin's before it starts.

The plain skill directories under `skills/` work with any agent that reads
the Agent Skills format.
