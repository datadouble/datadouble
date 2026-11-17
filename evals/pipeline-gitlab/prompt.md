---
max_turns: 30
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Bash, Skill]
---

Set up the whole Datadouble pipeline for datadouble/shop.yml in our GitLab CI.
Production jobs run on runners tagged prod-db, with the build's work directory
at /srv/datadouble/work; everything else runs on runners tagged staging.
