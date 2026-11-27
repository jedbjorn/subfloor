#!/usr/bin/env bash
# Run from the Subfloor source checkout: maintainer/draft-release.sh vYYYY.MM.DD COMMIT
set -euo pipefail

repo=jedbjorn/subfloor
source_url="https://github.com/$repo.git"
if [[ $# != 2 || ! $1 =~ ^v[0-9]+([.][0-9]+)+$ || ! $2 =~ ^[0-9a-f]{40}$ ]]; then
    echo "Usage: $0 vYYYY.MM.DD FULL_MAIN_COMMIT_SHA" >&2
    exit 2
fi
tag=$1
commit=$2
git fetch --no-tags "$source_url" refs/heads/main
if ! git merge-base --is-ancestor "$commit" FETCH_HEAD; then
    echo "The selected commit must be on Subfloor main." >&2
    exit 1
fi
existing_tag=$(git ls-remote --tags "$source_url" "refs/tags/$tag")
if [[ -n $existing_tag ]]; then
    echo "Tag $tag already exists. Choose an unused release tag." >&2
    exit 1
fi

stage=$(mktemp -d)
trap 'rm -rf -- "$stage"' EXIT
mkdir -p "$stage/bootstrap/.sc-state" "$stage/home"
git show "$commit:sc" > "$stage/bootstrap/sc"
chmod 755 "$stage/bootstrap/sc"
git show "$commit:LICENSE" > "$stage/bootstrap/.sc-state/subfloor-LICENSE"
printf '%s\n' "$commit" > "$stage/bootstrap/.sc-state/engine.ref"
printf '%s\n' "$source_url" > "$stage/bootstrap/.sc-state/engine.source"
asset=subfloor-bootstrap.tar.gz
tar -czf "$stage/$asset" -C "$stage/bootstrap" sc .sc-state

# Exercise the same launcher and revision a fresh project will use.
git init -q "$stage/project"
(
    cd "$stage/project"
    git remote add -t main super-coder "$source_url"
    tar --keep-old-files -xzf "$stage/$asset"
    env -i HOME="$stage/home" PATH="$PATH" ./sc install --help
    test "$(git rev-parse 'FETCH_HEAD^{commit}')" = "$commit"
    test -f .super-coder/scripts/install.py
)

cat > "$stage/notes.md" <<NOTES
This bootstrap installs Subfloor at commit \`$commit\`. Run the following in Bash from the primary checkout of a Linux Git project receiving Subfloor for the first time. The installer fetches and verifies the pinned engine, then prompts for setup.

\`\`\`bash
set -o pipefail
git remote add -t main super-coder $source_url &&
  curl -fsSL https://github.com/$repo/releases/download/$tag/$asset | tar --keep-old-files -xz &&
  ./sc install
\`\`\`

For a host runtime, use \`./sc install --runtime host\`. Follow the [quick start](https://github.com/$repo/blob/$commit/docs/quick-start.md) for prerequisites and the first launch.
NOTES
gh release create "$tag" "$stage/$asset" --repo "$repo" --target "$commit" \
    --title "Subfloor $tag" --notes-file "$stage/notes.md" --draft
