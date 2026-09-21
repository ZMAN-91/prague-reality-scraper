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
# WHY THE TAG NAMES THE WEEK IT HOLDS
#
# The archive is a snapshot of a week, so it is tagged with that week rather
# than with the week it was produced in. backup-2026-W38 holds the dataset as
# of Sunday 20 September, whether it was built on the Monday or on the
# Tuesday after a failed Monday - which is what makes the retry finish the
# job instead of starting a second archive one week over.
#
# It used to ask only whether a release with this week's tag existed. On
# 2026-09-20 one did: built on the Wednesday, from a dataset of 3129 listings
# that no longer existed - the dataset was rebuilt on the 17th and by Sunday
# held 10880. The tag was for the right week, so the backup was skipped, and
# because the tag would still be there the following Sunday it would have
# been skipped for ever.
#
# WHY THE ASSET'S DATE AND NOT THE RELEASE'S
#
# Re-running for a week that already has a release uploads over its assets,
# and neither `gh release upload` nor `gh release edit` moves publishedAt; it
# stays at whenever the tag was first cut. Read there, a refreshed archive
# still looks like the stale one. The assets carry their own upload time, and
# the asset IS the backup.
#
set -euo pipefail

NOW="${NOW:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
TODAY="${NOW:0:10}"
# The week that has ENDED, in Prague. `date -u +%G-W%V` names the week the
# run is in, which on Monday is the new one - six days of which have not
# happened yet.
TODAY_PRAGUE="$(TZ=Europe/Prague date -d "$NOW" +%F 2>/dev/null || TZ=Europe/Prague date +%F)"
CLOSED_SUNDAY="$(date -d "$TODAY_PRAGUE -$(date -d "$TODAY_PRAGUE" +%u) days" +%F)"
TAG="backup-$(date -d "$CLOSED_SUNDAY" +%G-W%V)"
UPLOADED=""
HAVE_FIXTURE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --uploaded) UPLOADED="$2"; HAVE_FIXTURE=1; shift 2 ;;
    --today) TODAY="$2"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$HAVE_FIXTURE" -eq 0 ]; then
  # A missing release is a 404, which is an answer, not a failure. A release
  # with no assets answers the same way an absent one does, and should: a
  # release whose upload failed is not a backup.
  UPLOADED="$(gh release view "$TAG" --repo "$GITHUB_REPOSITORY" \
    --json assets --jq '[.assets[].createdAt] | max // ""' 2>/dev/null || true)"
fi

if [ -z "$UPLOADED" ]; then
  echo "$TAG holds no archive yet." >&2
  echo "go=true"
  exit 0
fi

# Anything under this tag is this week's archive by definition - the tag
# names the week it holds. The Tuesday retry therefore finds Monday's work
# and stops, rather than rebuilding it.
echo "$TAG already holds an archive (uploaded $UPLOADED); nothing to do." >&2
echo "go=false"
