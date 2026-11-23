---
max_turns: 15
timeout_seconds: 600
allowed_tools: [Read, Glob, Grep, Bash, Skill, Edit, Write]
---

Our Datadouble build broke after the DBAs moved the shop database to Amazon
RDS and added PostGIS. It now fails before extract with:

    error: requirement_unmet: the working image lacks 2 requirement(s) of the sources; name an image that has them in sources.<name>.image, or leave them out with sources.<name>.omit
      sources.app.image: extension postgis is not in the working image
      sources.app.image: extension aws_s3 is not in the working image

The source runs PostgreSQL 18. Please fix datadouble/shop.yml so the build
passes.
