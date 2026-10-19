"""Gate checks a workflow runs as a command, shipped with the package.

A workflow file ships to every repository, so a check it names has to exist
in every repository too. A script under this repository's ``tools/`` does
not: in another repository the command fails to start, and its exit code is
read as a verdict. Checks that ask only git live here and are run with
``claunch gate <name>`` (:mod:`claude_launcher.cli_gate`).
"""
