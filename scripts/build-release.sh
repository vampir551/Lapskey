#!/usr/bin/env bash
#
# Builds release archives from the current git HEAD.
# Usage: ./scripts/build-release.sh [version]
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION="${1:-$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])')}"
VERSION="${VERSION#v}"

[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]] \
    || { echo "invalid version: $VERSION" >&2; exit 2; }

command -v git >/dev/null || { echo "git not found" >&2; exit 2; }
[[ -d .git ]] || { echo "run from a git checkout" >&2; exit 2; }

mkdir -p dist
rm -f dist/*.tar.gz dist/SHA256SUMS

git archive --format=tar.gz \
    --prefix="lapskey-$VERSION/" \
    -o "dist/lapskey-$VERSION.tar.gz" HEAD

# Stable name for releases/latest/download/lapskey.tar.gz
cp "dist/lapskey-$VERSION.tar.gz" "dist/lapskey.tar.gz"

(
    cd dist
    sha256sum "lapskey-$VERSION.tar.gz" "lapskey.tar.gz" > SHA256SUMS
)

echo "built:"
ls -lh dist
echo
cat dist/SHA256SUMS
