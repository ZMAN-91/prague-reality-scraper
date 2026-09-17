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
#   scrape_guard.sh --runs FILE     # read a fixture instead (what tests do)
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
EVENT_NAME="${EVENT_NAME:-schedule}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"

runs_file=""
if [ "${1:-}" = "--runs" ]; then
    runs_file="${2:?--runs needs a file}"
fi

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

if [ -n "$runs_file" ]; then
    runs=$(cat "$runs_file")
else
    # No --paginate: anything running or queued is among the newest runs by
    # definition, and this script runs ninety-six times a day.
    runs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?per_page=100")
fi

# Both scrape workflows write the same listings.csv, so either one holding it
# is a reason to stand down. Queued counts too, or this races the queue.
busy=$(printf '%s' "$runs" | jq "
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
last=$(printf '%s' "$runs" | jq -r "
    [ .workflow_runs[]
      | select(.id != ${GITHUB_RUN_ID})
      | select(.path == \".github/workflows/scrape.yml\")
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
