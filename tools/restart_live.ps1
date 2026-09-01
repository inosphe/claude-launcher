# Project-local live-service restart hook for Windows.
#
# Cflow runs this file from the driving session's CWD.  The workflow executor
# never assumes that its own daemon is the service target; this repository's
# hook deliberately chooses the claunch daemon.  A project that uses the
# workflow for another service replaces this file with its own restart action.

$ErrorActionPreference = 'Stop'

& claunch daemon restart
exit $LASTEXITCODE
