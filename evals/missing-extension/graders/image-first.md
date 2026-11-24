---
type: llm
---

The agent proposes, or sets, a `sources.<name>.image` that provides PostGIS on
PostgreSQL 18 (such as a `postgis/postgis` image), rather than dropping
PostGIS. For `aws_s3`, it explains that it is an Amazon RDS extension no
public image has, that `sources.<name>.omit` would leave it out of the copies and
that anything using it would then have to be excluded, and it leaves that
choice to the person instead of adding `omit` itself.
