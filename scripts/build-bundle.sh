#!/usr/bin/env bash
# Build the release bundle: scripts/build-bundle.sh <version> <sha7> <outdir>
#
# Writes <outdir>/bau-<version>.tar.gz and prints its path. Expects web/dist to be built already.
# Works in a temporary copy, so the checkout (bau/_build.py, bau/web_dist, pyproject.toml) is never touched.
set -euo pipefail

REGISTRY="registry.home.nakomis.com/nakomis"

die() {
    echo "build-bundle: $*" >&2
    exit 1
}

[[ $# -eq 3 ]] || die "usage: $0 <version> <sha7> <outdir>"
version=$1
sha=$2
outdir=$3

# Loose PEP 440: release, optional pre/post/dev segments, optional local label.
pep440='^[0-9]+(\.[0-9]+)*((a|b|rc)[0-9]+)?(\.post[0-9]+)?(\.dev[0-9]+)?(\+[a-z0-9]+([.][a-z0-9]+)*)?$'
[[ $version =~ $pep440 ]] || die "version '$version' is not a valid PEP 440 version (e.g. 0.1.3 or 0.0.0.dev42)"
[[ $sha =~ ^[0-9a-f]{7}$ ]] || die "sha '$sha' must be exactly 7 lowercase hex characters"

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
[[ -f "$repo/web/dist/index.html" ]] || die "web/dist is missing or empty; build the dashboard first (pnpm build in web/)"

mkdir -p "$outdir"
outdir=$(cd "$outdir" && pwd)

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

src="$work/src"
bundle="$work/bau-$version"
mkdir -p "$src" "$bundle/launchd"

# Source copy (no .gitignore, so hatch does not skip the generated files).
cp "$repo/pyproject.toml" "$repo/uv.lock" "$repo/README.md" "$repo/LICENSE" "$src/"
cp -R "$repo/bau" "$src/bau"
find "$src/bau" -name __pycache__ -type d -prune -exec rm -rf {} +
rm -rf "$src/bau/_build.py" "$src/bau/web_dist"

# 1. version
sed -E '/^\[project\]/,/^\[/ s/^version = ".*"$/version = "'"$version"'"/' "$src/pyproject.toml" >"$src/pyproject.toml.new"
mv "$src/pyproject.toml.new" "$src/pyproject.toml"
grep -qx "version = \"$version\"" "$src/pyproject.toml" || die "could not set the project version"

# 2. build stamp
printf 'VERSION = "%s"\nGIT_SHA = "%s"\n' "$version" "$sha" >"$src/bau/_build.py"

# 3. dashboard
cp -R "$repo/web/dist" "$src/bau/web_dist"

# 4. wheel
(cd "$src" && uv build --wheel --out-dir "$bundle")
rm -f "$bundle/.gitignore" # uv drops one into its out-dir
wheel="bau-$version-py3-none-any.whl"
[[ -f "$bundle/$wheel" ]] || die "expected $wheel, got: $(ls "$bundle")"

# 5. hashed runtime requirements
(cd "$src" && uv export --frozen --no-dev --no-emit-project --hashes) >"$bundle/requirements.txt"

cp "$repo"/launchd/com.nakomis.bau-{run,snapshot,api}.plist.in "$bundle/launchd/"
cp "$repo/container/compose.yml" "$bundle/compose.yml"

cat >"$bundle/INSTALL" <<INSTALL_EOF
uv venv --python 3.12 venv
uv pip install --python venv/bin/python --require-hashes -r requirements.txt
uv pip install --python venv/bin/python --no-deps $wheel
INSTALL_EOF

cat >"$bundle/VERSION" <<VERSION_EOF
BAU_VERSION=$version
BAU_SHA=$sha
BAU_AGENT_IMAGE=$REGISTRY/bau-agent:$version-$sha
BAU_EGRESS_IMAGE=$REGISTRY/bau-egress:$version-$sha
VERSION_EOF

if command -v sha256sum >/dev/null 2>&1; then
    sum() { sha256sum "$@"; }
else
    sum() { shasum -a 256 "$@"; }
fi
(
    cd "$bundle"
    find . -type f ! -name SHA256SUMS | sed 's|^\./||' | LC_ALL=C sort | while IFS= read -r f; do sum "$f"; done >SHA256SUMS
)

tarball="$outdir/bau-$version.tar.gz"
tar -czf "$tarball" -C "$work" "bau-$version"
echo "$tarball"
