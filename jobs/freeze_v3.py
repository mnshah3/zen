"""Freeze v3's inputs (v3 spec, section 15.2.4).

    python -m jobs.freeze_v3 copy   [--dest data/frozen/v3]
    python -m jobs.freeze_v3 seal   [--dest data/frozen/v3] --quality-factor PATH
    python -m jobs.freeze_v3 raw    [--dest data/frozen/v3] [--commit SHA]
    python -m jobs.freeze_v3 verify [--dest data/frozen/v3]

The freeze is a directory that mirrors the repository's relative paths:

    <dest>/data/zen.duckdb
    <dest>/data/financials/<every statement file>.parquet
    <dest>/data/reference/industry_nse.parquet
    <dest>/nse_list.json
    <dest>/data/external/nifty_tri.parquet
    <dest>/data/external/nifty_price_endpoints.csv
    <dest>/data/external/iima/<the IIMA four-factor CSV>
    <dest>/data/study/quality_factor.parquet
    <dest>/data/daily/**, data/daily_other/**, data/corpactions/*,
          data/announcements/**, data/indices/*   (the raw files, see `raw`)
    <dest>/freeze.json                    {relative path: sha256}, committed

`copy` copies every input except the quality factor and proves each copy equal
to its source by hash (and that the source did not change while it was
copied). The quality factor is then rebuilt from the frozen archive
(`jobs.build_quality_factor --db <dest>/data/zen.duckdb`), and `seal` adds it
and writes freeze.json. `verify` re-hashes the directory against freeze.json
and reports any file that is missing, changed or not listed. Every v3 program
takes `--frozen DIR` and may call `verify` before it reads anything.

`raw` (v3 spec, Clarification of 12 Oct 2026) adds the raw parquet files the
independent checker reads for prices, corporate actions, announcements and index
levels: like the v2 checker it never opens the database. They are taken from
git at RAW_COMMIT, the commit the database was frozen at, so they are the files
the frozen database was rebuilt from and anyone can reproduce them from the
repository. `raw` adds their hashes to freeze.json and changes no hash already
listed.

The statement files are exactly zen.data.financials.statement_files(), the
list the independent checker reads. The database is opened by nothing here:
it is copied as bytes. Nothing in this module reads a return.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zen.data import financials  # noqa: E402

DEST = ROOT / "data" / "frozen" / "v3"
FREEZE_JSON = "freeze.json"
QUALITY_FACTOR = "data/study/quality_factor.parquet"
IIMA_CSV = ("data/external/iima/"
            "2025-12_FourFactors_and_Market_Returns_Monthly_SurvivorshipBiasAdjusted.csv")
# Everything except the statement files (listed from financials.statement_files)
# and the quality factor (rebuilt from the frozen archive).
FIXED_INPUTS = ("data/zen.duckdb", "data/reference/industry_nse.parquet", "nse_list.json",
                "data/external/nifty_tri.parquet", "data/external/nifty_price_endpoints.csv",
                IIMA_CSV)
# The raw files the independent checker reads (jobs/crosscheck_v3.py), and the commit
# they are taken from: the repository as it stood when data/zen.duckdb was frozen.
RAW_DIRS = ("data/daily", "data/daily_other", "data/corpactions", "data/announcements",
            "data/indices")
RAW_COMMIT = "2be355ed51bd4d0eb8960241f74332136666e113"
_CHUNK = 8 << 20


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(_CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def statement_paths(root: Path) -> list[str]:
    """Relative posix paths of every financials statement file under `root`."""
    return [p.relative_to(root).as_posix()
            for p in financials.statement_files(root / "data" / "financials")]


def input_paths(root: Path, with_quality_factor: bool = False) -> list[str]:
    out = [*FIXED_INPUTS, *statement_paths(root)]
    if with_quality_factor:
        out.append(QUALITY_FACTOR)
    return out


def _copy_verified(src: Path, dst: Path) -> str:
    """Copy src to dst and return the hash both now have. The source is hashed
    before and after the copy: a file that changed under the copy (a daily job
    writing it) is refused rather than frozen half-written."""
    before = sha256(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    after = sha256(src)
    got = sha256(dst)
    if not (before == after == got):
        raise RuntimeError(f"{src}: changed while it was copied or the copy differs "
                           f"({before[:12]} / {after[:12]} / {got[:12]})")
    return got


def copy_inputs(dest: Path = DEST, root: Path = ROOT) -> dict[str, str]:
    """Copy every input except the quality factor; return {relative path: sha256}."""
    if (dest / FREEZE_JSON).exists():
        raise RuntimeError(f"{dest / FREEZE_JSON} exists: the freeze is sealed. If an input has "
                           "to change, remove the whole directory and redo step 4 and what "
                           "follows it (spec 15.2.4).")
    hashes = {}
    for rel in input_paths(root):
        src = root / rel
        if not src.is_file():
            raise FileNotFoundError(f"input missing: {src}")
        hashes[rel] = _copy_verified(src, dest / rel)
    return hashes


def seal(dest: Path = DEST, quality_factor: Path | None = None) -> dict[str, str]:
    """Add the rebuilt quality factor, hash every input, write freeze.json."""
    if (dest / FREEZE_JSON).exists():
        raise RuntimeError(f"{dest / FREEZE_JSON} exists: already sealed")
    if quality_factor is None or not Path(quality_factor).is_file():
        raise FileNotFoundError("seal needs --quality-factor, the file rebuilt from the "
                                "frozen archive")
    target = dest / QUALITY_FACTOR
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(quality_factor, target)
    hashes = {rel: sha256(dest / rel) for rel in input_paths(dest, with_quality_factor=True)}
    (dest / FREEZE_JSON).write_text(json.dumps(dict(sorted(hashes.items())), indent=1) + "\n",
                                    encoding="utf-8")
    return hashes


def add_raw(dest: Path = DEST, commit: str = RAW_COMMIT, root: Path = ROOT) -> dict[str, str]:
    """Extract RAW_DIRS as committed at `commit` into the sealed freeze and add
    their hashes to freeze.json. Returns the entries added.

    No hash already listed may change: a raw file whose path is already listed
    with another hash stops the run before freeze.json is rewritten."""
    import io
    import subprocess
    import tarfile
    dest = Path(dest)
    listed = load_freeze(dest)
    tar = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", commit, *RAW_DIRS],
                         check=True, capture_output=True).stdout
    added: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        members = [m for m in t.getmembers() if m.isfile()]
        for m in members:
            rel = Path(m.name).as_posix()
            if not any(rel.startswith(d + "/") for d in RAW_DIRS):
                raise RuntimeError(f"unexpected path in the archive: {rel}")
            data = t.extractfile(m).read()
            got = hashlib.sha256(data).hexdigest()
            if rel in listed and listed[rel] != got:
                raise RuntimeError(f"{rel} is already listed with another hash")
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            added[rel] = got
    if not added:
        raise RuntimeError(f"{commit} holds no file under {RAW_DIRS}")
    merged = {**listed, **added}
    (dest / FREEZE_JSON).write_text(json.dumps(dict(sorted(merged.items())), indent=1) + "\n",
                                    encoding="utf-8")
    return added


def load_freeze(dest: Path) -> dict[str, str]:
    return json.loads((Path(dest) / FREEZE_JSON).read_text(encoding="utf-8"))


def verify(dest: Path = DEST) -> list[str]:
    """Problems found re-hashing `dest` against its freeze.json; empty when sound.

    A file listed but missing or changed, and any file under `dest` that
    freeze.json does not list (an unhashed input a program could read), are all
    problems."""
    dest = Path(dest)
    problems: list[str] = []
    if not (dest / FREEZE_JSON).is_file():
        return [f"{dest / FREEZE_JSON} is missing"]
    listed = load_freeze(dest)
    for rel, want in listed.items():
        p = dest / rel
        if not p.is_file():
            problems.append(f"missing: {rel}")
        elif sha256(p) != want:
            problems.append(f"changed: {rel}")
    present = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    for rel in sorted(present - set(listed) - {FREEZE_JSON}):
        problems.append(f"not listed: {rel}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cmd", choices=["copy", "seal", "raw", "verify"])
    ap.add_argument("--dest", default=str(DEST))
    ap.add_argument("--commit", default=RAW_COMMIT,
                    help="raw: the commit the raw files are taken from")
    ap.add_argument("--quality-factor", default=None,
                    help="seal: the quality_factor.parquet rebuilt from the frozen archive")
    a = ap.parse_args(argv)
    dest = Path(a.dest)
    if a.cmd == "copy":
        h = copy_inputs(dest)
        print(f"copied {len(h)} files into {dest}")
        return 0
    if a.cmd == "seal":
        h = seal(dest, Path(a.quality_factor) if a.quality_factor else None)
        print(f"sealed {len(h)} files; wrote {dest / FREEZE_JSON}")
        return 0
    if a.cmd == "raw":
        h = add_raw(dest, a.commit)
        print(f"added {len(h)} raw files from {a.commit[:12]} to {dest / FREEZE_JSON}")
        return 0
    problems = verify(dest)
    for p in problems:
        print(p)
    print("freeze sound" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
