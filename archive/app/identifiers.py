"""External identifier normalisation + matching rules.

Priority (highest first):
  1. strong canonical external id of the *work* (goodgrid / douban / imdb /
     musicbrainz release-group ...): exact match => same work.
  2. version-level ids (isbn / upc / ean / release asin): same code => same
     concrete version, hence same work; different code => different version
     even if titles are identical.
  3. manual evidence link (evidence_url supplied by the site owner).
  4. title similarity: *never* auto-merges; it only opens a conflict for review.

Third-party pages (douban etc.) are reference clues only; a successful crawl is
NOT a licence to reproduce content.
"""

from __future__ import annotations

import re

# work-level: identity of the abstract work
WORK_NAMESPACES = {
    "goodgrid",          # fictional placeholder for a canonical book-id source
    "goodrid",
    "douban_work",
    "imdb",              # film title identity (tt...)
    "mb_releasegroup",   # musicbrainz release group
    "isbn_work",
}

# version-level: identity of one concrete edition/translation/recording
VERSION_NAMESPACES = {
    "isbn",
    "isbn10",
    "isbn13",
    "ean",
    "upc",
    "release_asin",
    "mb_release",
    "douban",
}

ALIAS = {
    "goodrid": "goodgrid",
    "goodgrid": "goodgrid",
    "goodreads": "goodgrid",
    "douban_work": "douban_work",
    "douban": "douban",
    "imdb": "imdb",
    "mb_releasegroup": "mb_releasegroup",
    "mb_release": "mb_release",
    "isbn_work": "isbn_work",
    "isbn13": "isbn",
    "isbn10": "isbn",
    "isbn": "isbn",
    "ean": "ean",
    "upc": "upc",
    "asin": "release_asin",
    "release_asin": "release_asin",
}


def norm_namespace(ns: str) -> str:
    ns = (ns or "").strip().lower().replace("-", "_")
    if ns not in ALIAS:
        # Unknown namespaces are accepted verbatim but treated as version
        # level unless explicitly registered as work-level.
        return ns
    return ALIAS[ns]


def is_work_level(ns: str) -> bool:
    return norm_namespace(ns) in WORK_NAMESPACES


def _isbn10_to_13(raw: str) -> str:
    digits = raw[:9]
    s = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate("978" + digits))
    check = (10 - s % 10) % 10
    return "978" + digits + str(check)


def norm_value(ns: str, value: str) -> str:
    ns = norm_namespace(ns)
    v = (value or "").strip()
    if ns == "isbn":
        digits = re.sub(r"[^0-9Xx]", "", v)
        if len(digits) == 10:
            digits = _isbn10_to_13(digits.upper())
        return digits
    if ns == "imdb":
        m = re.search(r"(\d+)", v)
        return "tt" + m.group(1).zfill(7) if m else v
    if ns in ("upc", "ean"):
        return re.sub(r"[^0-9]", "", v)
    return v


def parse_external_ids(cell: str) -> list[tuple[str, str]]:
    """Parse a CSV cell like 'isbn:978-7-5327-7753-2; imdb:tt0087803'.

    Split on the FIRST colon only; values may themselves contain ':' (URLs).
    """
    out: list[tuple[str, str]] = []
    if not cell:
        return out
    for part in re.split(r"[;；\n]", cell):
        part = part.strip()
        if not part or ":" not in part:
            continue
        ns, val = part.split(":", 1)
        ns, val = norm_namespace(ns), norm_value(ns, val)
        if ns and val:
            out.append((ns, val))
    return out


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", "", (title or "").lower())
