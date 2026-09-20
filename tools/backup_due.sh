#!/usr/bin/env bash
#
# Decide whether this attempt should build the weekly archive.
#
#   backup_due.sh                       # ask the API (what the workflow does)
#   backup_due.sh --published ISO --today YYYY-MM-DD
#                                       # read fixtures instead (what tests do).
#                                       # An empty ISO stands in for "no such
#                                       # release".
#
# Writes "go=true" or "go=false" to stdout, and the reason to stderr. The
# workflow appends stdout to $GITHUB_OUTPUT.
#
# Environment:
#   GH_TOKEN, GITHUB_REPOSITORY   for the API call
#   NOW                           ISO time, for tests; defaults to the clock
#
# WHY EXISTENCE IS NOT ENOUGH
#
# This asked only whether a release tagged for this ISO week existed. On
# 2026-09-20 one did: built on the Wednesday, from a dataset of 3129
# listings that no longer exists - the dataset was rebuilt on the 17th and
# by Sunday held 10880. The tag was for the right week, so the backup was
# skipped, and because the tag would still be there next Sunday it would
# have been skipped for ever. The only copy of the live data was a snapshot
# of something else, and nothing anywhere said so.
#
# The archive is the week's closing snapshot, so what counts is not that a
# release carries the week's name but that it was published on the day the
# window runs. A Wednesday test release for this week is not this week's
# backup.
set -euo pipefail

NOW="${NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
TODAY="${NOW:0:10}"
TAG="backup-$(date -u -d "$NOW" +%G-W%V 2>/dev/null || date -u +%G-W%V)"
PUBLISHED=""
HAVE_FIXTURE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --published) PUBLISHED="$2"; HAVE_FIXTURE=1; shift 2 ;;
    --today) TODAY="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$HAVE_FIXTURE" -eq 0 ]; then
  # A missing release is a 404, which is an answer, not a failure.
  PUBLISHED="$(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" \
    --json publishedAt --jq .publishedAt 2>/dev/null || true)"
fi

if [ -z "$PUBLISHED" ]; then
  echo "$TAG is not published yet." >&2
  echo "go=true"
  exit 0
fi

if [ "${PUBLISHED:0:10}" = "$TODAY" ]; then
  echo "$TAG was published today ($PUBLISHED); nothing to do." >&2
  echo "go=false"
  exit 0
fi

echo "$TAG exists but is from ${PUBLISHED:0:10}, not today ($TODAY) - it is not this week's snapshot." >&2
echo "go=true"
