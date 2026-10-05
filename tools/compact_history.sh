#!/usr/bin/env bash
#
# Replace the data branch's history with one commit holding today's data,
# after storing the full history where it can be restored from.
#
#   compact_history.sh <branch> [date]
#     DATA_ROOT       the data repository's checkout, FULL history (default .)
#     DRY_RUN=1       do everything except push and publish
#     GH              the gh binary (tests substitute a fake)
#
# WHY
#
# Every hourly commit rewrites listings.csv, and every day moves
# last_seen_at on every live row, so git keeps a delta of most of the table
# every day: the repository grew 4.6 MB a day in its third week, ~5 GB over
# three years. None of that history is needed to use the data - the weekly
# archives hold complete snapshots - but it is all cloned by every run.
# Decided 2026-10-05: compact once a year; run every six months, because a
# year of history at that rate (~1.7 GB) would come close to the 2 GiB limit
# on a release asset the bundle has to fit in.
#
# ORDER, AND WHY NOTHING CAN BE LOST
#
#   1. Bundle the whole history (git bundle --all) and verify the bundle.
#   2. Publish it as an asset of a release "history-<date>" whose tag points
#      at the CURRENT tip - so until step 5 the old history stays referenced
#      on GitHub whatever happens next.
#   3. Make one commit with the current tip's exact tree and no parent;
#      check the tree is identical.
#   4. Push it over the branch with --force-with-lease against the tip that
#      was bundled. If any run pushed in between, the lease fails and
#      nothing has changed: run it again later. (Concurrency groups are per
#      repository, so the scrapes in the code repository cannot be locked
#      out from here; the lease is what keeps their work.)
#   5. Move every tag - the weekly backup tags and the history tag - to the
#      new commit. Releases keep their assets when their tag moves. Only now
#      does the old history become unreferenced, and GitHub collects it in
#      its own time.
#
# A run that checked out before step 4 and commits after it still works:
# tools/commit_data.sh resets onto the branch tip (mixed) and stages only
# the files it changed, so it lands on top of the new root.
#
# To restore the old history:  git clone history-<date>.bundle

set -euo pipefail

BRANCH="${1:?usage: compact_history.sh <branch> [date]}"
DAY="${2:-$(date -u +%F)}"
DATA_ROOT="${DATA_ROOT:-.}"
GH="${GH:-gh}"
DRY_RUN="${DRY_RUN:-0}"
TAG="history-${DAY}"
BUNDLE="${BUNDLE_DIR:-${RUNNER_TEMP:-/tmp}}/prague-reality-history-${DAY}.bundle"

g() { git -C "${DATA_ROOT}" "$@"; }

if [ "$(g rev-parse --is-shallow-repository)" = "true" ]; then
  echo "::error::The checkout is shallow; the bundle would not hold the history." >&2
  exit 1
fi

g fetch --quiet --tags origin "${BRANCH}"
OLD="$(g rev-parse "origin/${BRANCH}")"
COMMITS="$(g rev-list --count "${OLD}")"
echo "Tip ${OLD}, ${COMMITS} commits."
if [ "${COMMITS}" -le 1 ]; then
  echo "Nothing to compact."
  exit 0
fi

# 1. The whole history, every branch and tag, verified.
g bundle create "${BUNDLE}" --all
g bundle verify "${BUNDLE}" >/dev/null
echo "Bundle $(du -h "${BUNDLE}" | cut -f1): ${BUNDLE}"
# A release asset may not exceed 2 GiB. Better to stop here, with nothing
# changed, than to replace the branch and fail to publish what it replaced.
if [ "$(stat -c %s "${BUNDLE}")" -ge "${MAX_BUNDLE_BYTES:-2000000000}" ]; then
  echo "::error::The bundle is too large for a release asset; nothing was changed. Compact more often." >&2
  exit 1
fi

# 3. One commit, the same tree, no parent.
TREE="$(g rev-parse "${OLD}^{tree}")"
NEW="$(g commit-tree "${TREE}" -m "Data as of ${DAY}; earlier history compacted

The full history up to ${OLD} is the release asset
prague-reality-history-${DAY}.bundle (release ${TAG}).
Restore it with: git clone prague-reality-history-${DAY}.bundle")"
if [ "$(g rev-parse "${NEW}^{tree}")" != "${TREE}" ]; then
  echo "::error::The new commit does not hold the same tree." >&2
  exit 1
fi
echo "New root ${NEW}, tree ${TREE} unchanged."

if [ "${DRY_RUN}" = "1" ]; then
  echo "Dry run: not publishing, not pushing."
  exit 0
fi

# 2. Publish the bundle with the history still referenced.
"${GH}" release create "${TAG}" "${BUNDLE}" --target "${OLD}" \
  --title "Data history up to ${DAY}" \
  --notes "Full git history of the data branch up to ${OLD}, before it was compacted to one commit. Restore with: git clone prague-reality-history-${DAY}.bundle"

# 4. Replace the branch, unless someone pushed since the bundle was made.
if ! g push --force-with-lease="refs/heads/${BRANCH}:${OLD}" origin "${NEW}:refs/heads/${BRANCH}"; then
  echo "::error::The branch moved while compacting; nothing was replaced. The history release ${TAG} stays as an extra copy; run again later." >&2
  exit 1
fi

# 5. Move every tag onto the new root, the history tag last.
for tag in $(g tag -l | grep -v "^${TAG}$" || true); do
  g push --force --quiet origin "${NEW}:refs/tags/${tag}"
done
g push --force --quiet origin "${NEW}:refs/tags/${TAG}"
echo "Compacted: ${COMMITS} commits -> 1. History in release ${TAG}."
