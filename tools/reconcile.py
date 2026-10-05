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
  + listings-    just refreshed it), except that first_seen_at keeps the
    archive/     earlier of the two and last_seen_at the later - those two
                 are the fields where the other run may legitimately know
                 more than we do. The live file and its monthly archives
                 are one table for this: a row the other run archived, or
                 brought back, is the same row whichever file it is in, and
                 the merged table is written back through storage so each
                 row lands in the one file its status puts it in.
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
from pathlib import Path, PurePosixPath

from common import storage
from common.schema import LISTING_FIELDS


def read_committed(ref: str, path: str, repo: Path = Path(".")) -> "str | None":
    """The committed contents of one file, or None if it is not in that ref.

    `repo` is the checkout the ref lives in, and `path` is relative to *its*
    root. Those are no longer the same thing as the working directory: when
    the code and the data live in separate repositories, this runs from the
    code checkout while the ref belongs to the data one.
    """
    # Bytes, decoded by hand: text=True would translate \r\n to \n, and the
    # CSVs this project writes end their lines in \r\n.
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
    )
    return result.stdout.decode("utf-8") if result.returncode == 0 else None


def merge_listings(ours_text: str, theirs_text: "str | None") -> str:
    """Union by internal_id; ours wins a row, but not its dates."""
    if not theirs_text:
        return ours_text

    theirs = {r["internal_id"]: r for r in csv.DictReader(io.StringIO(theirs_text))}
    ours = {r["internal_id"]: r for r in csv.DictReader(io.StringIO(ours_text))}
    merged = merge_listing_rows(ours, theirs)

    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=LISTING_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for internal_id in sorted(merged):
        writer.writerow(merged[internal_id])
    return out.getvalue()


def merge_listing_rows(ours: dict, theirs: dict) -> dict:
    """The rule behind merge_listings, on dicts keyed by internal_id."""
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
    return merged


def committed_files(ref: str, directory: str, repo: Path = Path(".")) -> list:
    """Paths of the files directly under `directory` in `ref`."""
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "--name-only", f"{ref}", f"{directory}/"],
        capture_output=True, text=True,
    )
    return result.stdout.split() if result.returncode == 0 else []


def reconcile_listings(base: str, path: Path, repo: Path) -> list:
    """Merge the other run's listings - live file and archives - into ours.

    Returns the changed files. Months the other run has an archive for are
    rewritten even when this run has no rows for them, so a month emptied
    here does not keep its rows in git (the commit stages no deletions).
    """
    try:
        relative = path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        relative = path.as_posix()
    archive_rel = (PurePosixPath(relative).parent / storage.archive_dir_for(path).name).as_posix()

    texts = []
    live = read_committed(base, relative, repo)
    if live is not None:
        texts.append(live)
    months = []
    for committed in committed_files(base, archive_rel, repo):
        month = PurePosixPath(committed).stem
        if not committed.endswith(".csv") or not storage.ARCHIVE_MONTH_RE.match(month):
            continue
        text = read_committed(base, committed, repo)
        if text is not None:
            texts.append(text)
            months.append(month)
    if not texts:
        return []

    theirs, _ = storage.union_rows(list(csv.DictReader(io.StringIO(t))) for t in texts)
    ours = storage.read_listings(path)
    return storage.write_listings(merge_listing_rows(ours, theirs), path,
                                  keep_archives=months)


def merge_appended(ours_text: str, theirs_text: "str | None", has_header: bool) -> str:
    """The other run's file as it is, then the lines only this run added.

    Byte for byte on their side, line endings included. This used to split
    and re-join on "\n", which turned every \r\n the csv module writes into
    \n - so each merge rewrote the whole monthly observations file (11 802
    lines changed on 2026-10-05 to add four), the next append added \r\n
    lines again, and the file ended up with both. Git stores every such
    rewrite in full, for ever.
    """
    if not theirs_text:
        return ours_text
    theirs_lines = theirs_text.splitlines(keepends=True)
    ours_lines = ours_text.splitlines(keepends=True)
    if has_header:
        ours_lines = ours_lines[1:]

    def key(line: str) -> str:
        return line.rstrip("\r\n")

    ending = "\r\n" if theirs_lines and theirs_lines[0].endswith("\r\n") else "\n"
    if theirs_lines and not theirs_lines[-1].endswith(("\n", "\r")):
        theirs_lines[-1] += ending
    seen = {key(line) for line in theirs_lines}
    merged = list(theirs_lines)
    for line in ours_lines:
        content = key(line)
        if content and content not in seen:
            seen.add(content)
            merged.append(line if line.endswith(("\n", "\r")) else line + ending)
    return "".join(merged)


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
              repo: Path = Path("."), changed: "set | None" = None) -> list:
    """Rewrite the working tree so it contains both runs' work. Returns notes.

    `repo` is the data repository's checkout. It defaults to the working
    directory, which is the single-repository layout, and is set to the
    data checkout when the two are split.

    `changed`, when given, is the set of repository paths this run itself
    changed, and nothing else is merged. A file this run did not touch is
    only a stale copy of what it checked out: merged with "ours wins" it
    would put the stale rows back over the other run's. That is how the
    hourly run of 2026-10-05 03:42 undid the weekly report committed forty
    minutes earlier. The listings table counts as touched when any of its
    files is, since a row moves between them.
    """
    notes: list = []
    repo = Path(repo)

    def relative_of(path: Path) -> str:
        # The ref knows the file by its path inside the data repository, which
        # is not where this process sees it when the two are split.
        try:
            return path.resolve().relative_to(repo.resolve()).as_posix()
        except ValueError:
            return path.as_posix()

    def mine(path: Path) -> bool:
        return changed is None or relative_of(path) in changed

    def handle(path: Path, merge, *args):
        if not path.exists() or not mine(path):
            return
        relative = relative_of(path)
        theirs = read_committed(base, relative, repo)
        if theirs is None:
            return
        with open(path, encoding="utf-8", newline="") as f:
            ours = f.read()
        merged = merge(ours, theirs, *args)
        if merged != ours:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(merged)
            notes.append(f"merged {relative}")

    listings = data_dir / "listings.csv"
    if listings.exists() and any(mine(p) for p in storage.listing_files(listings)):
        for written in reconcile_listings(base, listings, repo):
            notes.append(f"merged {relative_of(written)}")
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
    parser.add_argument("--changed", default=None,
                        help="file listing the repository paths this run "
                             "changed, one per line; only those are merged")
    args = parser.parse_args()

    changed = None
    if args.changed:
        changed = {line.strip() for line in
                   Path(args.changed).read_text(encoding="utf-8").splitlines() if line.strip()}
    notes = reconcile(args.base, Path(args.data_dir), Path(args.logs_dir),
                      Path(args.repo), changed)
    for note in notes:
        print(f"[reconcile] {note}")
    if not notes:
        print("[reconcile] nothing to merge; the base has no data this run does not")
    return 0


if __name__ == "__main__":
    sys.exit(main())
