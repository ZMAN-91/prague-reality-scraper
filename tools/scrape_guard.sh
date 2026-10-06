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
#   scrape_guard.sh --runs ALL [--mine MINE] [--jobs DIR]
#                                   # read fixtures instead (what tests do).
#                                   # ALL stands in for the repository-wide
#                                   # query, MINE for this workflow's own
#                                   # history; without --mine they are the
#                                   # same file. DIR/<run id>.json stands in
#                                   # for that run's job list; a run with no
#                                   # file there is judged by duration alone.
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
#   SCRAPE_JOB              the job that sweeps; a run where it was skipped
#                           was a guard saying no, however long it took
#                           (default scrape)
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
SCRAPE_JOB="${SCRAPE_JOB:-scrape}"
# Sixteen: four hours of fifteen-minute attempts. On 2026-10-05 eight long
# guard-only runs in a row sat on top of the real sweep; five was too few.
MAX_RUNS_CHECKED="${MAX_RUNS_CHECKED:-16}"
MEASURE_WORKFLOW="${MEASURE_WORKFLOW:-scrape.yml}"
EVENT_NAME="${EVENT_NAME:-schedule}"
GITHUB_RUN_ID="${GITHUB_RUN_ID:-0}"

runs_file=""
mine_file=""
jobs_dir=""
while [ $# -gt 0 ]; do
    case "$1" in
        --runs) runs_file="${2:?--runs needs a file}"; shift 2 ;;
        --mine) mine_file="${2:?--mine needs a file}"; shift 2 ;;
        --jobs) jobs_dir="${2:?--jobs needs a directory}"; shift 2 ;;
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
    # An API that does not answer is not a reason to fail the run. On
    # 2026-10-03 21:46 a 503 from GitHub ended this guard with exit 1, and a
    # failed run is a failure e-mail - for a hiccup on GitHub's side, with the
    # next attempt fifteen minutes away. Standing down is the safe answer:
    # not knowing whether a sweep is running is exactly when not to start
    # one, and an outage long enough to matter shows up in the health check
    # as a gap in the sweeps, which is the signal that should reach a person.
    if ! all_runs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs?per_page=100"); then
        decide false "Could not read the run history from the GitHub API - standing down; the next attempt asks again."
    fi
    if ! mine=$(gh api \
            "repos/${GITHUB_REPOSITORY}/actions/workflows/${MEASURE_WORKFLOW}/runs?per_page=50"); then
        decide false "Could not read this workflow's run history from the GitHub API - standing down; the next attempt asks again."
    fi
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
#
# Duration alone is not enough. On 2026-10-05 GitHub left guard jobs waiting
# ten and fifteen minutes for a runner; each then said no (or was cancelled
# unstarted), and each counted as a sweep because the run had lasted over
# MIN_REAL_RUN_SECONDS. The floor was measured from the last of those, and
# the 22:00 sweep never happened. So the long runs, newest first, are checked
# against their job list: one whose scrape job was skipped did not sweep.
# Only the newest MAX_RUNS_CHECKED are checked, and a run whose jobs
# cannot be read counts as a sweep - holding the floor closed for one cycle
# is the safe way to be wrong.
# For the same reason, if every run checked turns out not to have swept, the
# newest of them still sets the floor, as duration alone used to: running
# out of runs to check is not evidence that nothing swept.
candidates=$(printf '%s' "$mine" | jq -r "
    [ .workflow_runs[]
      | select(.id != ${GITHUB_RUN_ID})
      | select(.path == \".github/workflows/${MEASURE_WORKFLOW}\")
      | select(.status == \"completed\")
      | select(.run_started_at != null and .updated_at != null)
      | select((.updated_at | fromdateiso8601)
               - (.run_started_at | fromdateiso8601)
               > ${MIN_REAL_RUN_SECONDS}) ]
    | sort_by(.run_started_at) | reverse | .[:${MAX_RUNS_CHECKED}][]
    | \"\\(.id) \\(.run_started_at)\"")

swept() {
    local jobs
    if [ -n "$runs_file" ]; then
        [ -n "$jobs_dir" ] && [ -f "${jobs_dir}/$1.json" ] || return 0
        jobs=$(cat "${jobs_dir}/$1.json")
    else
        jobs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/$1/jobs") || return 0
    fi
    skipped=$(printf '%s' "$jobs" | jq -r "
        [ .jobs[]? | select(.name == \"${SCRAPE_JOB}\")
          | select(.conclusion == \"skipped\" or .conclusion == \"cancelled\" and .started_at == null) ]
        | length" 2>/dev/null) || return 0
    [ "${skipped:-0}" -eq 0 ]
}

last=""
newest=""
while read -r run_id started; do
    [ -n "$run_id" ] || continue
    [ -n "$newest" ] || newest="$started"
    if swept "$run_id"; then
        last="$started"
        break
    fi
    say "Run ${run_id} (${started}) lasted long but its ${SCRAPE_JOB} job never ran - not a sweep."
done <<< "$candidates"
[ -n "$last" ] || last="$newest"

if [ -z "$last" ]; then
    decide true "No previous real run on record - running."
fi

now_epoch=$(date -u -d "${NOW:-now}" +%s)
age=$(( (now_epoch - $(date -u -d "$last" +%s)) / 60 ))

if [ "$age" -lt "$MIN_GAP_MINUTES" ]; then
    decide false "Last real run started ${last}, ${age} min ago - under the ${MIN_GAP_MINUTES} min floor."
fi
decide true "Last real run started ${last}, ${age} min ago - running."
