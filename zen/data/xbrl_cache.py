"""A local copy of every XBRL filing, kept as it was downloaded.

WHY

Every parser fix so far has meant downloading the affected filings from NSE
again, which is slow, rate-limited, and depends on NSE still serving the same
bytes. With a local copy, a corrected parser re-reads from disk instead.

LAYOUT

    data/raw_xbrl/<first two hex chars>/<sha1 of the url>.xml.gz

Keyed by the URL, because the URL is what every table stores (`xbrl_url`) and
what a re-parse starts from. The two-character fan-out keeps any one directory
to a few thousand files. Stored as gzip because the documents are verbose XML:
on 1,640 cached sample filings the raw bytes averaged 96.5 KB and the gzip
6.4 KB, a fifteen-fold saving. The gzip header carries mtime=0, so the same
document always produces the same file.

RULES

* Only the body of a 200 response goes in, and only when it is well-formed
  XML. An HTML error page served with a 200 is not a filing; caching it would
  make the error permanent, because the cache never overwrites.
* A file, once written, is never overwritten. Writes go to a temporary file in
  the same directory and are then hard-linked into place, which fails rather
  than replaces if another writer got there first; either way the file at the
  final path is always complete.
* A copy that fails to decompress (a truncated disk write, say) is moved aside
  as `.corrupt` and treated as a miss, so it is downloaded again rather than
  blocking the slot for ever.

STALENESS

NSE names each document by filing id and upload timestamp (for example
INTEGRATED_FILING_INDAS_1450528_24052025113818_WEB.xml), and a revised filing
is a new document with a new URL, so a revision is a cache miss, not a stale
hit. The one case the cache cannot see is NSE replacing the bytes behind an
unchanged URL. `evict(url)` removes a copy so the next fetch downloads it
afresh; nothing in the cache is ever replaced silently.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

CACHE_DIR = Path("data/raw_xbrl")


def path_for(url: str, root: Path | None = None) -> Path:
    """Where the copy of `url` lives, whether or not it exists yet."""
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return Path(root if root is not None else CACHE_DIR) / h[:2] / f"{h}.xml.gz"


def get(url: str, root: Path | None = None) -> bytes | None:
    """The stored bytes for `url`, exactly as downloaded, or None on a miss."""
    p = path_for(url, root)
    try:
        with open(p, "rb") as f:
            packed = f.read()
    except OSError:
        # Missing, or unreadable (an antivirus or indexer lock, a directory at
        # that path). Either way it is a miss: the document is downloaded as it
        # would have been without a cache, instead of aborting a whole backfill.
        return None
    try:
        return gzip.decompress(packed)
    except (OSError, EOFError, zlib.error):
        # gzip's CRC caught a damaged copy. Move it aside, keep it for
        # inspection, and let the caller download a fresh one.
        try:
            os.replace(p, p.with_name(f"{p.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass
        return None


def _well_formed(content: bytes) -> bool:
    """Only an XBRL instance is worth keeping forever.

    A copy is never overwritten, so anything stored here is what every later
    run will read. An HTML error page, or a well-formed XML error document
    served with a 200, must not get in. Any parse problem at all (including an
    unknown declared encoding, which raises LookupError) means "do not store".
    """
    try:
        root = ET.fromstring(content)
    except Exception:                                            # noqa: BLE001
        return False
    return root.tag.endswith("}xbrl") or root.tag == "xbrl"


def put(url: str, content: bytes, root: Path | None = None) -> bool:
    """Store `content` for `url` unless a copy already exists.

    Returns True when this call wrote the file. Never raises for a cache
    problem: a full disk must not stop a download run, it only loses the copy.
    """
    if not content or not _well_formed(content):
        return False
    p = path_for(url, root)
    if p.exists():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".gz")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(gzip.compress(content, compresslevel=6, mtime=0))
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, p)          # fails if p exists: never overwrites
            except FileExistsError:
                return False
            except OSError:
                # Filesystem without hard links. os.rename refuses to replace
                # on Windows; on POSIX the exists() check above is the guard.
                if p.exists():
                    return False
                os.rename(tmp, p)
                return True
            return True
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    except OSError:
        return False


def evict(url: str, root: Path | None = None) -> bool:
    """Remove the copy of `url`, so the next fetch downloads it again."""
    try:
        path_for(url, root).unlink()
        return True
    except FileNotFoundError:
        return False
