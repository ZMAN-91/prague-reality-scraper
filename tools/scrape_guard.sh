#!/usr/bin/env bash
#
# Decide whether this scheduled attempt should actually scrape.
#
# GitHub's schedule event is best effort and drops most of them: asking for
# 24 runs a day at minute 0 delivered three, and minute 37 delivered none
# overnight. So the workflow asks four times an hour and throws away what it
# does not need. This is the throwing-away.
#
#   scrape_guard.sh                 # ask the API (what the workflow does)
#   scrape_guard.sh --runs ALL [--mine MINE]
#                                   # read fixtures instead (what tests do).
#                                   # ALL stands in for the repository-wide
#                                   # query, MINE for this workflow's own
#                                   # history; without --mine they are the
#                                   # same file.
#
# Writes "go=true" or "go=false" to stdout, and the reason to stderr. The
# workflow appends stdout to $GITHUB_OUTPUT.
#
# Environment:
#   GITHUB_RUN_ID           this run, so it does not count itself as busy
#   GITHUB_REPOSITORY       owner/name, for the API call
#   EVENT_NAME              github.event_name; anything but "schedule" runs
#   MIN_GAP_MINUTES         how old the last real run must be (default 50)
#   MIN_REAL_RUN_SECONDS    below this a run was a guard saying no (default 300)
#   MEASURE_WORKFLOW        whose last run sets the floor (default scrape.yml).
#                           The rent pass measures itself, on a weekly floor;
#                           what counts as "too soon" differs per workflow, but
#                           "something else is writing listings.csv" does not.
#   NOW                     ISO time, for tests; defaults to the clock
#
# WHY DURATION DECIDES WHAT COUNTS AS A RUN
#
# A run this script stops still appears in the run list, finished, seconds
# after it started. Counted as "the last run" it would hold the gap closed
# for another MIN_GAP_MINUTES, and the run after that, and so on forever -
# the collection would stop and every attempt would look like a success.
# Duration is what separates a real sweep from a guard saying no.

set -euo pipefail

MIN_GAP_MINUTES="${MIN_GAP_MINUTES:-50}"
MIN_REAL_RUN_SECONDS="${MIN_REAL_RUN_SECONDS:-300}"
MEASURE_WORKFLOW="${MEASURE_WORKFLOW:-scrape.yml}"
EVENT_NAME="${EVENT_NAME:-schedule}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"

runs_file=""
mine_file=""
while [ $# -gt 0 ]; do
    case "$1" in
        --runs) runs_file="${2:?--runs needs a file}"; shift 2 ;;
        --mine) mine_file="${2:?--mine needs a file}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

say() { echo "$1" >&2; }
decide() { echo "go=$1"; say "$2"; exit 0; }

# A person pressing the button is not the schedule being rationed. The
# concurrency group still serialises it against a running scrape, so this
# cannot race the data.
#
# An external waker is NOT a person: the Cloudflare Worker that keeps this
# collection alive fires every fifteen minutes, so it sets as_schedule and
# arrives here as EVENT_NAME=schedule. Without that it would put four sweeps
# an hour through the portals instead of one.
if [ "$EVENT_NAME" != "schedule" ]; then
    decide true "Triggered by ${EVENT_NAME}, not the schedule - running on request."
fi

# TWO QUERIES, NOT ONE, AND THE REASON MATTERS
#
# "Is anything running" wants the repository's newest runs, because anything
# in flight is by definition recent. "How old is the last real run" wants the
# one workflow's own history, because the waker fires ninety-six times a day
# and ninety-six runs is about ONE DAY of the repository-wide list. Asked
# there, the weekly rent pass would never find its previous run, conclude it
# had never run, and start again at every attempt in its window.
#
# Tests can pass a different fixture for each (--runs and --mine), which is
# the only way to catch the floor reading the wrong one.
if [ -n "$runs_file" ]; then
    all_runs=$(cat "$runs_file")
    mine=$(cat "${mine_file:-$runs_file}")
else
    all_runs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?per_page=100")
    mine=$(gh api \
        "repos/${GITHUB_REPOSITORY}/actions/workflows/${MEASURE_WORKFLOW}/runs?per_page=50")
fi

# Both scrape workflows write the same listings.csv, so either one holding it
# is a reason to stand down. Queued counts too, or this races the queue.
busy=$(printf '%s' "$all_runs" | jq "
    [ .workflow_runs[]
      | select(.id != ${GITHUB_RUN_ID})
      | select(.status == \"in_progress\" or .status == \"queued\")
      | select(.path == \".github/workflows/scrape.yml\"
            or .path == \".github/workflows/scrape-rent.yml\") ]
    | length")

if [ "$busy" -gt 0 ]; then
    decide false "A scrape is already running or queued - letting it finish."
fi

# The most recent run that actually swept. Nulls are skipped rather than
# crashing the guard: a malformed row must not stop the collection.
last=$(printf '%s' "$mine" | jq -r "
    [ .workflow_runs[]
      | select(.id != ${GITHUB_RUN_ID})
      | select(.path == \".github/workflows/${MEASURE_WORKFLOW}\")
      | select(.status == \"completed\")
      | select(.run_started_at != null and .updated_at != null)
      | select((.updated_at | fromdateiso8601)
               - (.run_started_at | fromdateiso8601)
               > ${MIN_REAL_RUN_SECONDS})
      | .run_started_at ]
    | sort | last // empty")

if [ -z "$last" ]; then
    decide true "No previous real run on record - running."
fi

now_epoch=$(date -u -d "${NOW:-now}" +%s)
age=$(( (now_epoch - $(date -u -d "$last" +%s)) / 60 ))

if [ "$age" -lt "$MIN_GAP_MINUTES" ]; then
    decide false "Last real run started ${last}, ${age} min ago - under the ${MIN_GAP_MINUTES} min floor."
fi
decide true "Last real run started ${last}, ${age} min ago - running."
