"""Merge another run's committed data into this run's working tree.

Two runs that overlap both read listings.csv at the start, both spend an hour
collecting, and both write the whole file back. Whoever pushes second finds
the file changed underneath them, and `git pull --rebase` cannot help: a
row-keyed CSV has no line-level merge that is correct, so git stops with a
conflict. The retry loop then re-ran the same pull while a rebase was already
in progress, which fails identically every time - so the weekly rent run
collected for an hour and threw all of it away.

Git is the wrong layer to merge this at. The data model has a key for every
row, so the merge is well defined here and nowhere else:

  listings.csv   union by internal_id. A row both runs have takes ours (we
                 just refreshed it), except that first_seen_at keeps the
                 earlier of the two and last_seen_at the later - those two
                 are the fields where the other run may legitimately know
                 more than we do.
  observations/  append-only; the union of the lines, in order. Nothing is
                 ever rewritten here, so there is no conflict to resolve.
  logs/          same, one JSON object per line.
  state, progress  small JSON; ours wins per key, theirs fills the gaps.
  raw/, csv/     ours. Raw archives are per-hour files that never collide,
                 and the CSV views are regenerated from listings.csv anyway.

Used by the commit step of both workflows. Reading the other side with
`git show <ref>:<path>` rather than from disk keeps this honest: it merges
what was actually committed, not whatever happens to be lying around.

    python -m tools.reconcile --base origin/main
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

from common.schema import LISTING_FIELDS


def read_committed(ref: str, path: str, repo: Path = Path(".")) -> "str | None":
    """The committed contents of one file, or None if it is not in that ref.

    `repo` is the checkout the ref lives in, and `path` is relative to *its*
    root. Those are no longer the same thing as the working directory: when
    the code and the data live in separate repositories, this runs from the
    code checkout while the ref belongs to the data one.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def merge_listings(ours_text: str, theirs_text: "str | None") -> str:
    """Union by internal_id; ours wins a row, but not its dates."""
    if not theirs_text:
        return ours_text

    theirs = {r["internal_id"]: r for r in csv.DictReader(io.StringIO(theirs_text))}
    ours = {r["internal_id"]: r for r in csv.DictReader(io.StringIO(ours_text))}

    merged = dict(theirs)
    for internal_id, row in ours.items():
        other = theirs.get(internal_id)
        if other is not None:
            # The other run may have seen this listing earlier, or more
            # recently, than we did. Everything else is ours: we just read it.
            if other.get("first_seen_at") and (
                not row.get("first_seen_at") or other["first_seen_at"] < row["first_seen_at"]
            ):
                row["first_seen_at"] = other["first_seen_at"]
            if other.get("last_seen_at", "") > row.get("last_seen_at", ""):
                row["last_seen_at"] = other["last_seen_at"]
        merged[internal_id] = row

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for internal_id in sorted(merged):
        writer.writerow(merged[internal_id])
    return out.getvalue()


def merge_appended(ours_text: str, theirs_text: "str | None", has_header: bool) -> str:
    """Union of the lines of an append-only file, order preserved."""
    if not theirs_text:
        return ours_text
    ours_lines = ours_text.splitlines()
    theirs_lines = theirs_text.splitlines()
    header: list = []
    if has_header and theirs_lines:
        header = [theirs_lines[0]]
        theirs_lines = theirs_lines[1:]
        ours_lines = ours_lines[1:] if ours_lines else []

    seen: set = set()
    body: list = []
    for line in theirs_lines + ours_lines:
        if line and line not in seen:
            seen.add(line)
            body.append(line)
    return "\n".join(header + body) + "\n"


def merge_json(ours_text: str, theirs_text: "str | None") -> str:
    """Shallow merge of two JSON objects; ours wins, theirs fills the gaps."""
    if not theirs_text:
        return ours_text
    try:
        ours = json.loads(ours_text)
        theirs = json.loads(theirs_text)
    except ValueError:
        return ours_text
    if not isinstance(ours, dict) or not isinstance(theirs, dict):
        return ours_text
    merged = {**theirs, **ours}
    return json.dumps(merged, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def reconcile(base: str, data_dir: Path, logs_dir: Path,
              repo: Path = Path(".")) -> list:
    """Rewrite the working tree so it contains both runs' work. Returns notes.

    `repo` is the data repository's checkout. It defaults to the working
    directory, which is the single-repository layout, and is set to the
    data checkout when the two are split.
    """
    notes: list = []
    repo = Path(repo)

    def handle(path: Path, merge, *args):
        if not path.exists():
            return
        # The ref knows the file by its path inside the data repository, which
        # is not where this process sees it when the two are split.
        try:
            relative = path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            relative = path.as_posix()
        theirs = read_committed(base, relative, repo)
        if theirs is None:
            return
        ours = path.read_text(encoding="utf-8")
        merged = merge(ours, theirs, *args)
        if merged != ours:
            path.write_text(merged, encoding="utf-8")
            notes.append(f"merged {relative}")

    handle(data_dir / "listings.csv", merge_listings)
    for observations in sorted((data_dir / "observations").glob("*.csv")):
        handle(observations, merge_appended, True)
    for log in sorted(logs_dir.glob("*.jsonl")):
        handle(log, merge_appended, False)
    handle(data_dir / "progress.json", merge_json)
    handle(data_dir / "state" / "last_observation.json", merge_json)
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="git ref holding the other run's data")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--logs-dir", default="logs")
    parser.add_argument("--repo", default=".",
                        help="checkout the base ref lives in (the data "
                             "repository, when code and data are split)")
    args = parser.parse_args()

    notes = reconcile(args.base, Path(args.data_dir), Path(args.logs_dir),
                      Path(args.repo))
    for note in notes:
        print(f"[reconcile] {note}")
    if not notes:
        print("[reconcile] nothing to merge; the base has no data this run does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
