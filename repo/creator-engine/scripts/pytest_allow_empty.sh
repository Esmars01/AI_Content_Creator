#!/usr/bin/env bash
# Runs pytest with the given arguments. Exit code 5 ("no tests collected") is reported
# honestly as "no tests yet" and treated as success, because suites are added phase by phase.
set -u
uv run pytest "$@"
code=$?
if [ "$code" -eq 5 ]; then
  echo "pytest $*: no tests collected yet (this suite is added in a later phase)."
  exit 0
fi
exit "$code"
