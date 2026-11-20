#!/usr/bin/env bash
# Every check CI runs that needs nothing but this checkout: ruff format --check, ruff
# check, mypy --strict (src, tests, scripts), pytest without the slow, openshell and
# substrate marks,
# the policies rendering to the committed files, and the web checks (typecheck, lint,
# tests, generated API types). Exits non-zero on the first failing check.
#
#   scripts/check.sh            # everything (the pre-push hook; CI runs the same and more)
#   scripts/check.sh --fast     # the pre-commit subset, under 3 minutes: ruff, mypy, the
#                               # unit tests (tests/unit, default marks), the policies, and
#                               # the web typecheck and lint; no API, property, web tests
#   scripts/check.sh --no-web   # skip web/ (for example without node_modules)
#
# The hooks in .githooks run it: pre-commit with --fast, pre-push in full. Enable them in
# this clone only with
#   git config core.hooksPath .githooks
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${root}"

web=1
fast=0
for arg in "$@"; do
  case "${arg}" in
    --no-web) web=0 ;;
    --fast) fast=1 ;;
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

step "identifier guard (no GCP billing or project number; env.sh values locally)" \
  uv run python scripts/public_scan.py --identifiers
step "ruff format --check" uv run ruff format --check
step "ruff check" uv run ruff check
step "mypy --strict" uv run mypy
if [[ "${fast}" -eq 1 ]]; then
  step "pytest tests/unit (not slow, openshell or substrate)" \
    uv run pytest -q -m "not slow and not openshell and not substrate" tests/unit
else
  step "pytest (not slow, openshell or substrate)" uv run pytest -q -m "not slow and not openshell and not substrate"
fi
step "policies render to the committed files" policies_current

if [[ "${web}" -eq 1 ]]; then
  step "web typecheck" npm --prefix web run --silent typecheck
  step "web lint" npm --prefix web run --silent lint
  if [[ "${fast}" -eq 0 ]]; then
    step "web tests" npm --prefix web run --silent test
    step "web generated API types" npm --prefix web run --silent check:api
  fi
fi

echo "check: all passed"
