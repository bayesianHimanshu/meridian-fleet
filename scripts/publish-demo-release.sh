#!/usr/bin/env bash
# Publish the demo as a GitHub release of the public repository (docs/demo/README.md,
# "Publishing"). For the project owner to run, from a clone of
# bayesianHimanshu/meridian-fleet:
#
#   scripts/publish-demo-release.sh TAG [ASSETS_DIR]
#
# ASSETS_DIR holds demo.mp4, captions.vtt and transcript.json as recorded (default
# var/demo). The script:
# - checks it is on a clean main whose HEAD is TAG, and that TAG exists on origin;
# - checks the three files against docs/demo/assets.sha256;
# - builds meridian-demo-TAG.zip (index.html and demo.mp4: the page works offline);
# - prints what it will upload and asks for "yes";
# - runs gh release create TAG --verify-tag, with docs/releases/TAG.md as the notes.
#
# It never creates or pushes a tag (--verify-tag refuses a tag missing on the remote),
# never changes a repository setting, and never reads or prints a token: gh uses your
# own login.
set -euo pipefail

REPO="bayesianHimanshu/meridian-fleet"
ASSETS=(demo.mp4 captions.vtt transcript.json)

die() { echo "publish-demo-release: $*" >&2; exit 2; }

[ $# -ge 1 ] && [ $# -le 2 ] || die "usage: $0 TAG [ASSETS_DIR]"
tag="$1"
assets_dir="${2:-var/demo}"
[[ "${tag}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "TAG must look like v1.1.0, not ${tag}"
cd "$(git rev-parse --show-toplevel)"

# 1. A clean main at TAG, and TAG on origin.
branch="$(git rev-parse --abbrev-ref HEAD)"
[ "${branch}" = "main" ] || die "on ${branch}, not main"
[ -z "$(git status --porcelain)" ] || die "the working tree is not clean"
git rev-parse -q --verify "refs/tags/${tag}^{commit}" >/dev/null || die "no local tag ${tag}"
[ "$(git rev-parse HEAD)" = "$(git rev-parse "${tag}^{commit}")" ] || die "HEAD is not ${tag}"
git ls-remote --exit-code --tags origin "refs/tags/${tag}" >/dev/null \
  || die "tag ${tag} is not on origin: push it first (git push origin ${tag})"
origin="$(git remote get-url origin)"
[[ "${origin}" == *"${REPO}"* ]] || die "origin is ${origin}, not ${REPO}"
notes="docs/releases/${tag}.md"
[ -f "${notes}" ] || die "no release notes at ${notes}"

# 2. The assets match the committed manifest.
manifest="$(pwd)/docs/demo/assets.sha256"
for f in "${ASSETS[@]}"; do
  [ -f "${assets_dir}/${f}" ] || die "missing ${assets_dir}/${f}"
done
(cd "${assets_dir}" && sha256sum --check --strict --quiet "${manifest}") \
  || die "the files in ${assets_dir} do not match docs/demo/assets.sha256"

# 3. The offline zip: the page and the video, side by side.
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT
zipname="meridian-demo-${tag}.zip"
mkdir -p "${work}/meridian-demo-${tag}"
cp docs/demo/index.html "${work}/meridian-demo-${tag}/index.html"
cp "${assets_dir}/demo.mp4" "${work}/meridian-demo-${tag}/demo.mp4"
(cd "${work}" && zip -q -X -r "${zipname}" "meridian-demo-${tag}")
uploads=("${assets_dir}/demo.mp4" "${assets_dir}/captions.vtt" "${assets_dir}/transcript.json" \
  "${work}/${zipname}")

# 4. Say what will happen, and ask.
echo "Release ${tag} of ${REPO}, notes ${notes}, files:"
for f in "${uploads[@]}"; do
  printf '  %s  %s bytes  sha256 %s\n' "$(basename "${f}")" "$(stat -c %s "${f}")" \
    "$(sha256sum "${f}" | cut -d' ' -f1)"
done
echo "The release is published at once; the Pages workflow then builds the site from it."
printf 'Type "yes" to create the release: '
read -r answer
[ "${answer}" = "yes" ] || die "not confirmed; nothing created"

# 5. Create it (gh, your login). --verify-tag: never creates a tag.
gh release create "${tag}" --repo "${REPO}" --verify-tag --title "Meridian Agent Fleet ${tag}" \
  --notes-file "${notes}" "${uploads[@]}"
echo "created; check the Pages workflow: gh run list --repo ${REPO} --workflow pages.yml"
