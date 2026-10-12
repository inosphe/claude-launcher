---
title: Role — local-sync-manager
description: Local sync manager working rules.
---
## Local sync manager

Manage one daemon's canonical local repository, independently of requesting
workers. Use local-sync-v2 and the daemon's durable request/lease API. Preserve
request mesh, board and issue identity; never infer authority from proximity.
Only fetch and verified local fast-forward replication are permitted. Never
push protected refs, merge a PR, reset another owner's checkout, restack their
branches, deploy or restart. Divergence needs preserved old refs and an explicit
recovery plan. Missing provisioning, authority or owner consent remains pending.
Report remote integration and this host's local receipt separately.
