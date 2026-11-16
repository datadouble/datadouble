# Agent skills

Datadouble ships a Claude Code plugin whose skills set up a pipeline, or any
part of one, in a team's own repositories. This repository is its
marketplace:

```
/plugin install datadouble --marketplace datadouble/datadouble
```

Then ask for what you need: "set up the Datadouble pipeline for
datadouble/shop.yml in our GitLab CI", "copy the avatars from our uploads too",
"roll staging back to yesterday's image".

| Skill | Sets up |
|---|---|
| `datadouble-pipeline` | Everything, in order, with one summary at the end |
| `datadouble-spec` | The spec, checked with `datadouble plan` |
| `datadouble-files` | File trees, with each `find` expression explained and tried first |
| `datadouble-build`, `datadouble-promote` | The production jobs |
| `datadouble-fetch`, `datadouble-provision`, `datadouble-release`, `datadouble-prune`, `datadouble-activate` | The non-production jobs |

Every skill follows the same rules (`skills/RULES.md`): CI/CD configuration
comes only from `datadouble ci init`, safety settings are never weakened to
make a run pass, production jobs never go on a vendor-hosted runner, and the
skills never handle secrets or connect to production. Asked to break one of
these, the agent explains why it won't and what to do instead.

The skills are released with Datadouble; each checks that `datadouble
version` matches the plugin's before it starts. The plain skill directories
under `skills/` work with any agent that reads the Agent Skills format.
