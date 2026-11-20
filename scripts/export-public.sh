#!/usr/bin/env bash
# Export the private repository's main as the tree of the new public repository
# (docs/demo/README.md, "Publishing", step 3). For the project owner to run, in the private
# clone, after the release-pages PR is merged:
#
#   scripts/export-public.sh VERSION [REF] [DEST]
#
# VERSION names the public release (v1.1.0). REF is what to export (default main). DEST is a
# new empty directory outside this repository (default: a fresh one under ${TMPDIR:-/tmp}).
#
# 1. git archive REF into DEST: tracked files only; no history, no .git, nothing ignored or
#    untracked (so no var/, deploy/gke/env.sh, design/ or meridian-sealed/).
# 2. The public scan on DEST (scripts/public_scan.py --full): GCP identifiers by exact match
#    against this clone's deploy/gke/env.sh; secret shapes; real email addresses; home paths;
#    billing and 12-digit patterns in deploy/, docs/, src/; files over 5 MB. Any match stops it.
# 3. uv sync, the full checks (scripts/check.sh) and the web production build, in a
#    throwaway copy of DEST, with a temporary git repository (check.sh compares rendered
#    policies with git). DEST itself is left exactly as exported.
# 4. Prints the commands that create the public repository. It never runs them: no git init in
#    DEST, no commit, no gh repo create, no push, no tag.
set -euo pipefail

die() { echo "export-public: $*" >&2; exit 2; }

[ $# -ge 1 ] && [ $# -le 3 ] || die "usage: $0 VERSION [REF] [DEST]"
version="$1"
ref="${2:-main}"
[[ "${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "VERSION must look like v1.1.0"
repo="$(git rev-parse --show-toplevel)"
cd "${repo}"
commit="$(git rev-parse --verify "${ref}^{commit}")" || die "no ${ref}"
env_file="${repo}/deploy/gke/env.sh"
[ -f "${env_file}" ] || die "no ${env_file}: the scan needs it to refuse the real identifiers"

if [ $# -ge 3 ]; then
  dest="$(realpath -m "$3")"
else
  dest="$(mktemp -d "${TMPDIR:-/tmp}/meridian-fleet-public.XXXXXX")/meridian-fleet"
fi
case "${dest}/" in "${repo}/"*) die "${dest} is inside the repository" ;; esac
if [ -e "${dest}" ] && [ -n "$(ls -A "${dest}")" ]; then die "${dest} is not empty"; fi
mkdir -p "${dest}"

echo "== 1. export ${ref} (${commit:0:12}) to ${dest}"
git archive --format=tar "${commit}" | tar -x -C "${dest}"
echo "   $(find "${dest}" -type f | wc -l) files, $(du -sh "${dest}" | cut -f1)"

echo "== 2. public scan of the export"
python3 "${repo}/scripts/public_scan.py" --full --root "${dest}" --env-file "${env_file}" \
  || die "the export holds something that must not be public (above); nothing else was done"

echo "== 3. uv sync, the full checks and the web build, in a throwaway copy"
work="$(mktemp -d "${TMPDIR:-/tmp}/meridian-fleet-check.XXXXXX")"
trap 'rm -rf "${work}"' EXIT
cp -a "${dest}/." "${work}/"
(
  cd "${work}"
  git -c init.defaultBranch=main init -q
  git -c user.name=export-check -c user.email=export-check@example.invalid \
    -c commit.gpgsign=false add -A
  git -c user.name=export-check -c user.email=export-check@example.invalid \
    -c commit.gpgsign=false commit -q -m "export check"
  uv sync --locked
  (cd web && npm ci --no-audit --no-fund)
  scripts/check.sh
  (cd web && npx vite build)
)
echo "   checks and web build passed (the throwaway copy is removed)"

cat <<EOF

== 4. The export is ready: ${dest}
Run these yourself (this script runs none of them):

  cd ${dest}
  git init -b main
  git config user.name "bayesianHimanshu"
  git config user.email "<id>+bayesianHimanshu@users.noreply.github.com"   # your noreply address
  git add -A
  git commit -m "Meridian Agent Fleet ${version}"
  gh repo create bayesianHimanshu/meridian-fleet --public --source . --remote origin
  git push -u origin main
  git tag ${version}
  git push origin ${version}

Then, in that clone: Settings > Pages > Source: GitHub Actions, and
scripts/publish-demo-release.sh ${version} ${repo}/var/demo
EOF
