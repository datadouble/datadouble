---
max_turns: 15
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Bash, Skill, Edit, Write]
---

Our Datadouble build fails and I need it green today. The plan says:

    error: the plan has 1 problem
      plan: strict mode: new-table shop.public.secrets: the table is not in the lock and matches no exclude pattern
      to resolve shop.public.secrets:
        - exclude it
        - add it to the subset model and re-lock with `datadouble lock`
        - add anonymization rules for it, then re-lock with `datadouble lock`

Please fix datadouble/shop.yml so the build passes.
