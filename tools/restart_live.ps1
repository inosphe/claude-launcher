# Project-local live-service restart hook for Windows.
#
# Cflow runs this file from the driving session's CWD.  The workflow executor
# never assumes that its own daemon is the service target; this repository's
# hook deliberately chooses the claunch daemon.  A project that uses the
# workflow for another service replaces this file with its own restart action.
#
# The decision lives in tools/restart_live.py: it asks tools/deploy_check.py
# whether the running daemon already serves the branch's code and restarts
# only when it serves older code.  A round that merged nothing (a tip that
# differs only in .beads/) restarts nothing.

$ErrorActionPreference = 'Stop'

& uv run --no-sync python tools/restart_live.py --branch master
exit $LASTEXITCODE
