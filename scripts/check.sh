#!/usr/bin/env bash
# Every check CI runs that needs nothing but this checkout: ruff format --check, ruff
# check, mypy --strict (src, tests, scripts), pytest without the slow and openshell marks,
# the policies rendering to the committed files, and the web checks (typecheck, lint,
# tests, generated API types). Exits non-zero on the first failing check.
#
#   scripts/check.sh            # everything
#   scripts/check.sh --no-web   # skip web/ (for example without node_modules)
#
# The pre-commit hook in .githooks runs it; enable it in this clone only with
#   git config core.hooksPath .githooks
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root}"

web=1
for arg in "$@"; do
  case "${arg}" in
    --no-web) web=0 ;;
    *) echo "check.sh: unknown argument ${arg}" >&2; exit 2 ;;
  esac
done

step() {
  local name="$1"
  shift
  echo "check: ${name}"
  if ! "$@"; then
    echo "check: FAILED: ${name}" >&2
    exit 1
  fi
}

# set -e does not apply inside a function called from `if`, so every command is chained.
# The rendered files must equal the working tree's (staged or committed), and none may be
# left untracked: so a commit that changes a policy passes once the new files are added.
policies_current() {
  uv run meridian policy render >/dev/null \
    && git diff --quiet --exit-code -- policies \
    && test -z "$(git ls-files --others --exclude-standard -- policies)"
}

step "ruff format --check" uv run ruff format --check
step "ruff check" uv run ruff check
step "mypy --strict" uv run mypy
step "pytest (not slow, not openshell)" uv run pytest -q -m "not slow and not openshell"
step "policies render to the committed files" policies_current

if [[ "${web}" -eq 1 ]]; then
  step "web typecheck" npm --prefix web run --silent typecheck
  step "web lint" npm --prefix web run --silent lint
  step "web tests" npm --prefix web run --silent test
  step "web generated API types" npm --prefix web run --silent check:api
fi

echo "check: all passed"
