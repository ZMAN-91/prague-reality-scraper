#!/usr/bin/env bash
#
# Commit and push one run's data, on top of whatever landed on the branch
# while the run was collecting.
#
# Both scrape workflows had this inline and identical, which is how the bug
# below survived review in two places at once. It lives here so it can be
# tested against a real git repository (tests/test_commit_step.py).
#
# Usage: tools/commit_data.sh <branch> [label]
#   DATA_ROOT      where the data repository is checked out (default ".")
#   COMMIT_ATTEMPTS / RETRY_SLEEP   overridden by the tests
#
# --- the two rules this file exists to enforce -----------------------------
#
# 1. NEVER `git reset --soft`. It moves HEAD but leaves the index holding
#    this run's original checkout, so the resulting commit carries a revert
#    of every file that changed on the branch meanwhile. A run that overlaps
#    a code push would silently undo it. This actually happened: the weekly
#    rent run of 2026-09-16 tried to commit a revert of two workflow files
#    and was saved only by GitHub refusing to let a token without `workflows`
#    permission touch them - had the change been Python, it would have gone
#    through and quietly reverted it.
#
#    A mixed reset (the default) moves HEAD *and* the index to the new tip
#    while leaving the working tree alone, so the `git add` below stages this
#    run's data and nothing else.
#
# 2. NEVER let `git add` stage a deletion. After the mixed reset the index
#    knows about files the other run added - a raw archive, a new monthly
#    observations file - that this run's working tree never had. A plain
#    `git add data/` reads those as deletions and the commit removes another
#    run's work. `--ignore-removal` stages additions and modifications only,
#    which is the whole of what a scrape run legitimately does: it never
#    deletes a data file.
#
# Neither rule is visible in a passing run. Both show up as data quietly
# going missing, which is why they are asserted in tests rather than trusted
# to a comment.

set -euo pipefail

BRANCH="${1:?usage: commit_data.sh <branch> [label]}"
LABEL="${2:-Scrape run}"
DATA_ROOT="${DATA_ROOT:-.}"
ATTEMPTS="${COMMIT_ATTEMPTS:-5}"
RETRY_SLEEP="${RETRY_SLEEP:-5}"

git -C "${DATA_ROOT}" config user.name "prague-reality-scraper-bot"
git -C "${DATA_ROOT}" config user.email "actions@users.noreply.github.com"

# Rule 3: commit what THIS run changed, and nothing else.
#
# After the reset below, every file that differs from the branch tip gets
# staged by a plain `git add` - including files this run never touched, whose
# working copies are simply what it checked out an hour ago. Those are
# stale, and committing them reverts whoever changed them since. On
# 2026-10-05 the hourly run that started at 03:01 committed at 03:42 and put
# back the previous week's REPORT.md and data/csv/ over the weekly report
# committed at 03:02. While the CSV views were rebuilt every hour nobody
# noticed; once they were rebuilt nightly, every overlap lost them.
#
# So the list is taken once, here, before the first reset, against the
# commit this run checked out: modified tracked files plus new ones. Only
# those are staged, and only those are handed to reconcile.
MINE="$(mktemp)"
trap 'rm -f "${MINE}"' EXIT
# And the commit itself, so reconcile can tell a row this run removed from a
# row the other run added (tools/reconcile.merge_listing_rows).
START="$(git -C "${DATA_ROOT}" rev-parse HEAD)"
{
  git -C "${DATA_ROOT}" diff --name-only HEAD
  git -C "${DATA_ROOT}" ls-files --others --exclude-standard
} | sort -u > "${MINE}"

for attempt in $(seq 1 "${ATTEMPTS}"); do
  git -C "${DATA_ROOT}" fetch --quiet origin "${BRANCH}"

  # Rule 1: mixed, not --soft.
  git -C "${DATA_ROOT}" reset --quiet "origin/${BRANCH}"

  # Merge the other run's rows in at the layer that has a key for every row.
  # Never `git pull --rebase`: a row-keyed CSV has no correct line-level
  # merge, so git stops on a conflict and the retry loop re-runs the same
  # pull while a rebase is already in progress - which is how the first
  # weekly rent run threw away an hour of collection.
  reconciled=$(python -m tools.reconcile \
    --base "origin/${BRANCH}" \
    --repo "${DATA_ROOT}" \
    --data-dir "${DATA_ROOT}/data" \
    --logs-dir "${DATA_ROOT}/logs" \
    --changed "${MINE}" \
    --start-ref "${START}")
  echo "${reconciled}"

  # Rule 2: additions and modifications only - every path staged here exists.
  # What is staged: this run's own changes, plus whatever reconcile rewrote
  # (a listings archive month the other run touched, merged with ours). Only
  # under data/, logs/, reports/ and REPORT.md - the places a run writes.
  # REPORT.md and reports/<week>.md are the report job's; the weekly copy was
  # once never staged at all, and report_due decides by it.
  {
    cat "${MINE}"
    printf '%s\n' "${reconciled}" | sed -n 's/^\[reconcile\] merged //p'
  } | sort -u | while IFS= read -r path; do
    case "${path}" in
      data/*|logs/*|reports/*|REPORT.md) ;;
      *) continue ;;
    esac
    if [ -f "${DATA_ROOT}/${path}" ]; then
      git -C "${DATA_ROOT}" add --ignore-removal -- "${path}"
    fi
  done

  # Everything else in the working tree is a stale checkout copy of a file
  # someone else has since changed. Bring it up to the branch, so later steps
  # in this job read the current files and a retry starts from the truth.
  git -C "${DATA_ROOT}" checkout -- .

  if git -C "${DATA_ROOT}" diff --cached --quiet; then
    echo "No data changes to commit this run."
    exit 0
  fi

  git -C "${DATA_ROOT}" commit -q -m "${LABEL} $(date -u +'%Y-%m-%dT%H:%M:%SZ')"

  if output=$(git -C "${DATA_ROOT}" push origin "HEAD:${BRANCH}" 2>&1); then
    echo "${output}"
    exit 0
  fi
  echo "${output}"

  # Only a lost race is worth another attempt. Anything else - a permission
  # refusal, a protected branch, a bad credential - repeats identically, and
  # retrying it just buries the real error under five copies of itself. That
  # is exactly what the rent run did: five attempts, one cause, two minutes
  # spent proving it.
  if ! printf '%s' "${output}" |
       grep -qE 'fetch first|non-fast-forward|behind its remote|stale info'; then
    echo "::error::Push was rejected for a reason retrying cannot fix. See above." >&2
    exit 1
  fi

  echo "Push attempt ${attempt} lost a race; re-merging against the new head."
  sleep $((attempt * RETRY_SLEEP))
done

echo "Failed to push after ${ATTEMPTS} attempts." >&2
exit 1
