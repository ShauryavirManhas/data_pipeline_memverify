#!/usr/bin/env bash
# Rebuild the mock task repos (tasks/*/repo) from their git bundles.
# Git cannot store a repo inside a repo, so each mock repo is committed as
# tasks/<name>/repo.bundle. Run once after cloning, from the project root:
#   bash scripts/setup_mock_tasks.sh
set -euo pipefail
cd "$(dirname "$0")/.."
for bundle in tasks/*/repo.bundle; do
  dir="$(dirname "$bundle")/repo"
  if [ -d "$dir/.git" ]; then
    echo "ok      $dir (already there)"
    continue
  fi
  rm -rf "$dir"
  git clone -q "$bundle" "$dir"
  head="$(git -C "$dir" rev-parse HEAD)"
  echo "created $dir at $head"
done
