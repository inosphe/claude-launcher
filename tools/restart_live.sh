#!/usr/bin/env bash
# Project-local live-service restart hook for Linux and WSL.
#
# The cflow workflow invokes this file from the session CWD.  Its executor is
# target-agnostic; this repository deliberately uses the claunch daemon as the
# live service.  Other projects provide their own script at this path.
#
# The decision lives in tools/restart_live.py: it asks tools/deploy_check.py
# whether the running daemon already serves the branch's code and restarts
# only when it serves older code.  A round that merged nothing (a tip that
# differs only in .beads/) restarts nothing.

set -euo pipefail

exec uv run --no-sync python tools/restart_live.py --branch master
