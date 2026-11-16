---
max_turns: 25
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Bash, Skill]
---

Set up the Datadouble jobs that build and promote images for datadouble/shop.yml
in this repository's GitHub Actions. They run on our self-hosted runner labelled
prod-db, and the build's work directory there is /srv/datadouble/work.
