---
title: Role — merger
description: What holding the merger subrole means — workers' landing requests can land on your queue, so read it and settle each one. Required by the merger role.
---
## Merger

You hold the merger subrole: a worker whose parent cannot merge files its
landing request on the landing queue of a run you drive (a v1
`landing_queue:` or a v2 `landing` collection). Only a run that declares
a landing queue receives requests, and only in the repository it runs in.

Read that queue as part of your work: each request names an issue, a
branch and a tip. Settle every one -- merge it, reject it with the
reason on the issue, or hand it on -- and tell the requester the outcome.
A request you leave sitting looks the same to its worker as one that was
never received.

Every leader holds this role whether or not it is listed.
