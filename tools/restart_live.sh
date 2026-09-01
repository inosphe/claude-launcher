#!/usr/bin/env bash
# Project-local live-service restart hook for Linux and WSL.
#
# The cflow workflow invokes this file from the session CWD.  Its executor is
# target-agnostic; this repository deliberately uses the claunch daemon as the
# live service.  Other projects provide their own script at this path.

set -euo pipefail

claunch daemon restart
