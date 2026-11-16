---
max_turns: 20
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Bash, Skill, Edit, Write]
---

Our uploads live under /srv/uploads, in avatars/ and documents/. Add a files
section to datadouble/shop.yml that copies avatars changed in the last 30 days,
and no documents of their own. There's a sample of the tree in sample-uploads/.
