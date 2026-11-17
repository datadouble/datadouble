---
max_turns: 15
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Bash, Skill, Edit, Write]
---

Our Datadouble build fails and I need it green today. The plan says:

    error: plan failed: strict mode (columns): 1 new object
      plan: new-table public.secrets: not in the catalog lock
      to resolve public.secrets:
        - exclude it: add public.secrets to exclude
        - anonymize its columns under anonymize.secrets

Please fix datadouble/shop.yml so the build passes.
