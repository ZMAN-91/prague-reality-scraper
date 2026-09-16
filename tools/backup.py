"""Weekly snapshot of the dataset, verified by actually restoring it.

The data is already pushed to GitHub every hour, so this is not protection
against a bad run - git history covers that, and you can walk back to any
hour. What a weekly archive adds is the case git does *not* cover: the
repository itself going away (deleted, locked out, an account problem), and
the case where someone wants the dataset as one openable file rather than as
a clone plus a checkout.

WHAT GOES IN, AND WHAT DOES NOT

    data/listings.csv        one row per listing ever seen - the spine
    data/observations/       every price/status change, append-only
    data/changes/            every other edit a listing made to itself
    data/state/              last observed value per listing
    data/progress.json       where each source's sweep stopped
    data/csv/                the browsable views, so a restore is usable
                             immediately without running anything
    logs/                    one JSON object per run

    data/raw/                NOT included - see below

`data/raw/` is left out on purpose, and it is the one judgement call in this
file. It is the archive of the portals' actual HTTP responses, and it is
~80% of the bytes: 13 MB after four runs, growing by a megabyte or two a day
for ever (the index dump is once-per-day, details are once-per-listing, but
neither is ever deleted). Copying all of it into a fresh archive every week
means re-uploading the same unchanged gzip files 52 times a year, and the
total passes a gigabyte inside a year. Meanwhile it is append-only, so git
history already holds every version of every one of those files, and nothing
in it is needed to reconstruct the dataset - listings.csv and observations/
are the parsed result, not a cache of it. Raw is the audit trail: worth
keeping in the repository, not worth 52 copies.

So: the archive restores the *dataset*. It does not restore the audit trail.
That is stated in the manifest rather than left for someone to discover.

VERIFICATION

Every file's SHA-256 goes into the manifest, and `--verify` extracts the
archive to a temporary directory and re-hashes all of it, then parses
listings.csv with the project's own reader and checks the row count against
what the manifest claims. The workflow runs that against every archive it
builds, before uploading it. An archive nobody has ever restored is not a
backup, it is a file.

    python -m tools.backup --out backup/          build, manifest, verify
    python -m tools.backup --verify backup/x.tar.gz
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# What the archive holds, in the order it is written. A directory is taken
# whole; a file is taken as itself. Anything missing is simply skipped - a
# fresh repository has no observations yet, and that is not an error.
DATASET = (
    "data/listings.csv",
    "data/observations",
    "data/changes",
    "data/state",
    "data/progress.json",
    "data/csv",
    "logs",
)

# Excluded, with the reason carried into the manifest so a restore does not
# have to guess whether the absence is deliberate.
EXCLUDED = {
    "data/raw": (
        "Append-only archive of the portals' raw HTTP responses. Kept in git "
        "(every version of every file is in the history) but not copied into "
        "weekly archives: it is ~80% of the bytes, it never changes once "
        "written, and the dataset does not depend on it."
    ),
}

CHUNK = 1 << 20


class BackupError(RuntimeError):
    """A backup that cannot be verified. Raised rather than reported, so a
    caller has to deal with it and a workflow step fails loudly."""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def dataset_files(root: Path, members=DATASET) -> list[Path]:
    """Every file the archive should hold, as paths relative to `root`.

    Sorted, so two runs over the same tree produce the same manifest order
    and a diff between two manifests is readable.
    """
    found: list[Path] = []
    for member in members:
        target = root / member
        if target.is_dir():
            found.extend(
                p.relative_to(root) for p in target.rglob("*") if p.is_file()
            )
        elif target.is_file():
            found.append(Path(member))
    return sorted(set(found))


def current_commit(root: Path) -> str | None:
    """The commit the archive was taken from, so a restore can be compared
    against the repository. None outside a git checkout - which happens in
    tests and is not worth failing over."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    return out.stdout.strip() or None


def count_rows(path: Path) -> int:
    """Data lines in a CSV, i.e. not counting the header. Used for the
    manifest's own record of what it should contain after a restore."""
    if not path.is_file():
        return 0
    with path.open("r", encoding="utf-8", newline="") as f:
        return max(sum(1 for _ in f) - 1, 0)


def build_manifest(root: Path, files: list[Path], now: datetime,
                   commit: str | None = None) -> dict:
    """Everything needed to check a restore without trusting the archive."""
    entries = [
        {
            "path": str(rel).replace("\\", "/"),
            "bytes": (root / rel).stat().st_size,
            "sha256": sha256_of(root / rel),
        }
        for rel in files
    ]
    observations = sorted(
        e["path"] for e in entries if e["path"].startswith("data/observations/")
    )
    return {
        "format": 1,
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "week": now.strftime("%G-W%V"),
        "commit": commit,
        "files": entries,
        "total_bytes": sum(e["bytes"] for e in entries),
        "counts": {
            "files": len(entries),
            "listings": count_rows(root / "data/listings.csv"),
            "observation_months": len(observations),
            "observations": sum(
                count_rows(root / p) for p in observations
            ),
        },
        "excluded": EXCLUDED,
    }


RESTORE_TEXT = """\
Prague reality - weekly dataset snapshot
========================================

Week {week}, taken {generated_at} from commit {commit}.
{listings} listings, {observations} observations, {files} files.

To restore into a checkout:

    tar xzf {name} --strip-components=1 -C /path/to/repo

To check the archive before trusting it:

    python -m tools.backup --verify {name}

This archive holds the DATASET (listings, observations, state, progress and
the browsable CSV views in data/csv/). It does NOT hold data/raw/, the
archive of the portals' raw HTTP responses - that lives in git history only.
See tools/backup.py for why.

data/csv/ is derived; `python -m tools.export_csv` regenerates it from
data/listings.csv and data/observations/ if it is ever in doubt.
"""


def archive_name(week: str) -> str:
    return f"prague-reality-{week}.tar.gz"


def write_archive(root: Path, out_dir: Path, now: datetime | None = None,
                  members=DATASET) -> tuple[Path, dict]:
    """Build the .tar.gz plus a manifest.json beside it.

    The manifest goes *inside* the archive (so a restore is self-describing)
    and *next to* it (so the release page shows what is in there without
    anyone downloading a tarball to find out).
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    files = dataset_files(root, members)
    if not files:
        raise BackupError(
            f"nothing to back up under {root} - refusing to write an empty "
            f"archive, which would look like a successful backup"
        )
    manifest = build_manifest(root, files, now, current_commit(root))
    week = manifest["week"]
    prefix = f"prague-reality-{week}"

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / archive_name(week)
    readme = RESTORE_TEXT.format(
        week=week,
        generated_at=manifest["generated_at"],
        commit=manifest["commit"] or "(not a git checkout)",
        listings=manifest["counts"]["listings"],
        observations=manifest["counts"]["observations"],
        files=manifest["counts"]["files"],
        name=out_path.name,
    )

    with tarfile.open(out_path, "w:gz") as tar:
        for rel in files:
            tar.add(root / rel, arcname=f"{prefix}/{rel}")
        _add_bytes(tar, f"{prefix}/MANIFEST.json",
                   json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"), now)
        _add_bytes(tar, f"{prefix}/RESTORE.txt", readme.encode("utf-8"), now)

    (out_dir / f"{prefix}.manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return out_path, manifest


def _add_bytes(tar: tarfile.TarFile, arcname: str, body: bytes,
               now: datetime) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(body)
    info.mtime = int(now.timestamp())
    info.mode = 0o644
    import io

    tar.addfile(info, io.BytesIO(body))


def _extract_all(tar: tarfile.TarFile, dest: Path) -> None:
    """Extract with the `data` filter, which refuses absolute paths and
    anything escaping the destination. Falls back for Pythons that predate
    the filter argument rather than silently extracting unfiltered."""
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        for member in tar.getmembers():
            if member.name.startswith("/") or ".." in Path(member.name).parts:
                raise BackupError(f"unsafe path in archive: {member.name}")
            if not (member.isfile() or member.isdir()):
                raise BackupError(f"unexpected member type: {member.name}")
        tar.extractall(dest)


def verify_archive(path: Path) -> dict:
    """Restore the archive to a temporary directory and check all of it.

    Not a checksum of the tarball - that only proves the file downloaded
    cleanly. This unpacks it, re-hashes every member against the manifest,
    and then reads listings.csv with the project's own reader to confirm the
    restored dataset parses and holds the number of rows the manifest says.
    """
    path = Path(path)
    if not path.is_file():
        raise BackupError(f"no such archive: {path}")

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp)
        with tarfile.open(path, "r:gz") as tar:
            _extract_all(tar, dest)

        roots = [p for p in dest.iterdir() if p.is_dir()]
        if len(roots) != 1:
            raise BackupError(
                f"expected exactly one top-level directory, found {len(roots)}"
            )
        root = roots[0]

        manifest_path = root / "MANIFEST.json"
        if not manifest_path.is_file():
            raise BackupError("archive has no MANIFEST.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        for entry in manifest["files"]:
            restored = root / entry["path"]
            if not restored.is_file():
                raise BackupError(f"missing from archive: {entry['path']}")
            actual = sha256_of(restored)
            if actual != entry["sha256"]:
                raise BackupError(
                    f"corrupt: {entry['path']} hashes {actual[:12]}, "
                    f"manifest says {entry['sha256'][:12]}"
                )
            if restored.stat().st_size != entry["bytes"]:
                raise BackupError(f"wrong size: {entry['path']}")

        # The restore has to be *usable*, not merely intact. Reading it back
        # through the project's own loader is what tells us the CSV is still
        # a CSV this code can open, rather than a file with the right hash.
        from common import storage

        listings = storage.read_listings(root / "data/listings.csv")
        expected = manifest["counts"]["listings"]
        if len(listings) != expected:
            raise BackupError(
                f"restored listings.csv holds {len(listings)} rows, "
                f"manifest says {expected}"
            )

        return {
            "archive": str(path),
            "bytes": path.stat().st_size,
            "week": manifest["week"],
            "commit": manifest["commit"],
            "files": len(manifest["files"]),
            "listings": len(listings),
            "observations": manifest["counts"]["observations"],
            "uncompressed_bytes": manifest["total_bytes"],
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root to back up")
    parser.add_argument("--out", default="backup",
                        help="directory to write the archive into")
    parser.add_argument("--verify", metavar="ARCHIVE",
                        help="verify an existing archive instead of building one")
    parser.add_argument("--github-output", action="store_true",
                        help="append name=value lines to $GITHUB_OUTPUT")
    args = parser.parse_args(argv)

    try:
        if args.verify:
            summary = verify_archive(Path(args.verify))
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            print(f"OK: {summary['archive']} restores cleanly "
                  f"({summary['listings']} listings)", file=sys.stderr)
            return 0

        out_path, manifest = write_archive(Path(args.root), Path(args.out))
        summary = verify_archive(out_path)
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(
            f"OK: {out_path} ({summary['bytes'] / 1e6:.1f} MB compressed, "
            f"{summary['uncompressed_bytes'] / 1e6:.1f} MB raw, "
            f"{summary['listings']} listings) built and verified",
            file=sys.stderr,
        )
        if args.github_output:
            _emit_outputs(out_path, summary)
        return 0
    except BackupError as exc:
        print(f"BACKUP FAILED: {exc}", file=sys.stderr)
        return 1


def _emit_outputs(out_path: Path, summary: dict) -> None:
    import os

    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as f:
        f.write(f"archive={out_path}\n")
        f.write(f"week={summary['week']}\n")
        f.write(f"listings={summary['listings']}\n")
        f.write(f"observations={summary['observations']}\n")
        f.write(f"bytes={summary['bytes']}\n")


if __name__ == "__main__":
    raise SystemExit(main())
