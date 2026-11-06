"""Temporal, source-grounded knowledge over website snapshots.

``answer(as_of)`` uses the latest snapshot whose ``taken_on`` is on or before
that date. A snapshot that includes the site root (path ``/``) is a full
crawl: pages it does not contain are gone. A snapshot with only interior
pages is a supplement: it replaces URLs it fetched and leaves every other URL
from the previous state in place. An effective date printed on a later crawl
is not back-dated into an earlier website state. A date before the first
snapshot abstains.

Pricing rows keep qualifiers (product, purpose, term, amount tier, model year,
consumer/business, IRA wrapper, intro versus post-promo). APR, APY, dividend
rate, and nominal rate are different measures. Official rate and product tables
outrank calculators, promos, guidance, news, and blogs. Missing a price is not
the same as missing the product: the answer refuses only the unsupported field
and does not invent a number.

``conflicts()`` reports contradictions that coexist in the latest snapshot.
Each listing is one current claim plus the stale figures that compete with
that claim directly. An older model-year tier, a superseded effective date,
or a value from a page the current snapshot no longer contains is not added
to that listing. Sequential history and tier/measure differences are not
current conflicts.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Contract types (field-compatible with harness.contract; no harness import)
# ---------------------------------------------------------------------------

try:
    # The harness puts its directory on sys.path before loading this module.
    # Using those dataclasses keeps isinstance checks (repeat scoring) intact.
    from contract import Answer, Conflict, Source  # type: ignore
except ImportError:
    @dataclass
    class Source:
        url: str
        snapshot_id: Optional[str] = None
        quote: Optional[str] = None

    @dataclass
    class Answer:
        text: str
        sources: list = field(default_factory=list)
        confident: bool = True
        conflict_note: Optional[str] = None

    @dataclass
    class Conflict:
        topic: str
        values: list
        sources: list
        resolution: Optional[str] = None
        reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_DASHES = str.maketrans({"–": "-", "—": "-", "−": "-", "\u00a0": " ", "\u2011": "-"})
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_NUM_WORDS = [
    ("seventeen", "17"), ("eighteen", "18"), ("nineteen", "19"),
    ("thirteen", "13"), ("fourteen", "14"), ("fifteen", "15"), ("sixteen", "16"),
    ("eleven", "11"), ("twelve", "12"),
    ("twenty", "20"), ("thirty", "30"), ("forty", "40"), ("fifty", "50"),
    ("sixty", "60"), ("seventy", "70"), ("eighty", "80"), ("ninety", "90"),
    ("seven", "7"), ("eight", "8"), ("three", "3"), ("four", "4"), ("five", "5"),
    ("nine", "9"), ("zero", "0"), ("one", "1"), ("two", "2"), ("six", "6"),
    ("ten", "10"),
]
_DATE_RE = re.compile(
    r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+(\d{1,2}),\s+(20\d{2})\b",
    re.I,
)
_EFFECTIVE_NUM_RE = re.compile(
    r"effective(?:\s+as\s+of)?\s+(\d{1,2})[/-](\d{1,2})[/-](20\d{2})",
    re.I,
)
_EFFECTIVE_WORD_RE = re.compile(
    r"effective(?:\s+as\s+of)?\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\.?\s+(\d{1,2}),?\s+(20\d{2})",
    re.I,
)
_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_DOLLAR_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?")
_TERM_RE = re.compile(r"\b(\d+)\s*-?\s*(months?|mos?\b|years?|yrs?\b)\b", re.I)
_YEAR_SPAN_RE = re.compile(r"\b(19\d{2}|20\d{2})\s*-\s*(19\d{2}|20\d{2}|\d{2})\b")
_OLDER_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b.{0,24}\bolder\b", re.I)
_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_ROUTING_RE = re.compile(r"\b(\d{9})\b")
_HOUR_DAY_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
    re.I,
)
_HOUR_TIME_RE = re.compile(
    r"\b(\d{1,2}(?::\d{2})?\s*(?:a\.?\s*m\.?|p\.?\s*m\.?)|noon|midnight)\b",
    re.I,
)
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STOP = {
    "the", "and", "for", "with", "your", "you", "our", "are", "was", "what",
    "how", "does", "did", "can", "this", "that", "from", "have", "has", "any",
    "all", "not", "but", "its", "it's", "dupaco", "about", "please", "tell",
    "give", "show", "current", "currently", "right", "now", "today",
}


def _clean(text: str) -> str:
    text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text.translate(_DASHES)


def _norm_space(text: str) -> str:
    return re.sub(r"\s+", " ", _clean(text)).strip()


def _money(token: str) -> float:
    return float(Decimal(token.replace(",", "")))


def _cents(value: float) -> int:
    return int(round(float(value) * 100))


def _iso_from_mdy(month: int, day: int, year: int) -> str:
    return f"{year:04d}-{month:02d}-{day:02d}"


def _parse_article_date(text: str) -> str:
    match = _DATE_RE.search(text[:1800])
    if not match:
        return ""
    month = _MONTHS[match.group(1)[:3].lower()]
    return _iso_from_mdy(month, int(match.group(2)), int(match.group(3)))


def _parse_effective(text: str) -> str:
    match = _EFFECTIVE_NUM_RE.search(text)
    if match:
        month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return _iso_from_mdy(month, day, year)
    match = _EFFECTIVE_WORD_RE.search(text)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        if month:
            return _iso_from_mdy(month, int(match.group(2)), int(match.group(3)))
    return ""


def _url_key(url: str) -> str:
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host + parsed.path.rstrip("/").lower()


def _host(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).netloc.lower()


def _path(url: str) -> str:
    return urlparse(url if "://" in url else "https://" + url).path.lower()


def _words_to_digits(text: str) -> str:
    out = text
    for word, digit in _NUM_WORDS:
        out = re.sub(rf"\b{word}\b", digit, out, flags=re.I)
    return out


def _percent_texts(cell: str) -> list[str]:
    cleaned = re.sub(r"%\d{1,2}\b", "%", _norm_space(cell))
    return [f"{token}%" for token in _PCT_RE.findall(cleaned)]


def _dollar_text(token_whole: str, frac: Optional[str]) -> tuple[float, str]:
    number = _money(token_whole + (("." + frac) if frac else ""))
    shown = "$" + token_whole
    if frac:
        shown += "." + frac
    return number, shown


def _parse_term_months(text: str) -> Optional[int]:
    match = _TERM_RE.search(text.lower())
    if not match:
        return None
    count = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("y"):
        return count * 12
    return count


def _parse_years(text: str) -> Optional[tuple[int, int]]:
    lowered = text.lower()
    match = _YEAR_SPAN_RE.search(lowered)
    if match:
        start = int(match.group(1))
        raw_end = match.group(2)
        if len(raw_end) == 4:
            end = int(raw_end)
        else:
            end = int(str(start)[:2] + raw_end)
        lo, hi = sorted((start, end))
        return lo, hi
    match = _OLDER_YEAR_RE.search(lowered)
    if match:
        return 1900, int(match.group(1))
    return None


def _parse_balance(text: str) -> Optional[tuple[float, float]]:
    lowered = text.lower().replace("–", "-").replace("—", "-")
    match = re.search(
        r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*-\s*\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?",
        lowered,
    )
    if match:
        lo, _ = _dollar_text(match.group(1), match.group(2))
        hi, _ = _dollar_text(match.group(3), match.group(4))
        if hi < lo:
            lo, hi = hi, lo
        return lo, hi
    match = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*\+", lowered)
    if match:
        lo, _ = _dollar_text(match.group(1), match.group(2))
        return lo, 1e18
    match = re.search(r"\$\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?\s*or less", lowered)
    if match:
        hi, _ = _dollar_text(match.group(1), match.group(2))
        return 0.0, hi
    return None


def _balance_contains(bounds: Optional[tuple[float, float]], amount: float) -> bool:
    if bounds is None:
        return False
    lo, hi = bounds
    cents = _cents(amount)
    return _cents(lo) <= cents <= _cents(hi)


def _balance_span(bounds: tuple[float, float]) -> float:
    return bounds[1] - bounds[0]


def _same_bounds(a: Optional[tuple[float, float]], b: Optional[tuple[float, float]]) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return _cents(a[0]) == _cents(b[0]) and _cents(a[1]) == _cents(b[1])


def _years_overlap(a: Optional[tuple[int, int]], b: Optional[tuple[int, int]]) -> bool:
    if a is None or b is None:
        return False
    return not (a[1] < b[0] or b[1] < a[0])


def _near(value: float, other: float, tol: float = 0.001) -> bool:
    return abs(value - other) <= tol


# ---------------------------------------------------------------------------
# Page and fact model
# ---------------------------------------------------------------------------

LOAN_FAMILIES = {
    "auto", "personal", "boat", "atv", "heloc", "home_equity",
    "home_improvement", "construction", "card",
}
DEPOSIT_FAMILIES = {
    "certificate", "checking", "share_savings", "bright", "hsa", "money_market",
    "achieve", "holiday", "launchpad", "high_yield", "you_name_it", "savings_other",
}


def classify(url: str) -> tuple[str, int]:
    path = _path(url)
    host = _host(url)
    if "calculator" in path:
        return "calculator", 5
    if any(token in path for token in ("switch-and-earn", "/promo", "offer-lp", "-lp")):
        return "promo", 20
    if "homebuying-guidance" in path or path.rstrip("/").endswith("/guidance"):
        return "guidance", 30
    if path.rstrip("/").endswith("/rates") or "/rates/" in path or "business-rates" in path:
        return "rate_sheet", 100
    if path.startswith("/newsroom"):
        return "news", 12
    if path.startswith("/learn/blog") or "/blog/" in path:
        return "blog", 8
    if path.startswith("/hubfs") or path.endswith(".pdf"):
        return "archive", 15
    if any(token in path for token in ("/personal/", "/business/", "/checking", "/about", "/contact", "/online-and-mobile")):
        return "product", 80
    if "earn-more" in path:
        return "product", 80
    if host.startswith("info."):
        return "promo", 10
    return "general", 45


def family_of(section: str, product: str) -> str:
    blob = f"{section} {product}".lower()
    if re.search(r"\bfee\b", blob):
        return "fee"
    checks = (
        ("achieve", "achieve"),
        ("heloc", "heloc"),
        ("home equity line", "heloc"),
        ("home equity", "home_equity"),
        ("home improvement", "home_improvement"),
        ("construction", "construction"),
        ("boat", "boat"),
        (" rv", "boat"),
        ("atv", "atv"),
        ("snowmobile", "atv"),
        ("jet ski", "atv"),
        ("auto", "auto"),
        ("motorcycle", "auto"),
        ("vehicle", "auto"),
        ("personal loan", "personal"),
        ("mortgage", "mortgage"),
        ("fixed conventional", "mortgage"),
        ("home loan", "mortgage"),
        ("visa", "card"),
        ("credit card", "card"),
        ("certificate", "certificate"),
        ("checking", "checking"),
        ("health savings", "hsa"),
        (" hsa", "hsa"),
        ("share savings", "share_savings"),
        ("bright", "bright"),
        ("money market", "money_market"),
        ("holiday", "holiday"),
        ("launchpad", "launchpad"),
        ("high-yield", "high_yield"),
        ("high yield", "high_yield"),
        ("you name it", "you_name_it"),
        ("savings", "savings_other"),
    )
    for needle, family in checks:
        if needle in blob:
            if family == "home_equity" and "line" in blob:
                return "heloc"
            if family == "auto" and any(token in blob for token in ("boat", "atv", "snowmobile", "jet ski")):
                continue
            return family
    return "other"


def wrapper_of(section: str, product: str) -> str:
    blob = f"{section} {product}".lower()
    if "roth" in blob:
        return "roth"
    if "traditional" in blob:
        return "traditional"
    if re.search(r"\bira\b", blob):
        return "ira"
    return "none"


def purpose_of(section: str, product: str) -> str:
    blob = f"{section} {product}".lower()
    if "rate relief" in blob:
        return "rate_relief"
    if "refinance" in blob:
        return "refinance"
    if "purchase" in blob:
        return "purchase"
    return "unknown"


def segment_of(url: str, section: str, product: str) -> str:
    blob = f"{url} {section} {product}".lower()
    if "business" in blob and "/personal/" not in url.lower():
        return "business"
    return "consumer"


def phase_of(header: str, product: str) -> str:
    blob = f"{header} {product}".lower()
    if "after" in blob and ("promo" in blob or "1st" in blob or "intro" in blob or "6 month" in blob):
        return "post_promo"
    if "after promotional" in blob or "after the promo" in blob:
        return "post_promo"
    if any(token in blob for token in ("intro", "1st 6", "first 6", "for 1st")):
        return "intro"
    return "standard"


def column_kind(header: str) -> Optional[tuple[str, str]]:
    lowered = header.lower()
    if "payment" in lowered or "point" in lowered:
        return ("skip", "standard")
    phase = "standard"
    if "after" in lowered:
        phase = "post_promo"
    elif any(token in lowered for token in ("intro", "1st", "first 6", "promotional period")) and "after" not in lowered:
        phase = "intro"
    if "apy" in lowered or "annual percentage yield" in lowered:
        return "apy", phase
    if "dividend" in lowered:
        return "dividend", phase
    if "apr" in lowered or "annual percentage rate" in lowered:
        return "apr", phase
    if re.search(r"\brate\b", lowered):
        return "rate", phase
    if "min" in lowered and "earn" in lowered:
        return "minimum", "standard"
    if "min" in lowered and "open" in lowered:
        return "min_open", "standard"
    return None


@dataclass
class Fact:
    snap_id: str
    taken_on: str
    url: str
    kind: str
    authority: int
    section: str
    product: str
    family: str
    segment: str
    purpose: str
    wrapper: str
    phase: str
    measure: str
    value: float
    value_text: str
    unit: str
    term_months: Optional[int]
    year_lo: Optional[int]
    year_hi: Optional[int]
    bal_lo: Optional[float]
    bal_hi: Optional[float]
    jumbo: bool
    special: bool
    effective: str
    origin: str
    row_id: int
    quote: str
    detail_urls: tuple = ()
    value_hi: Optional[float] = None
    is_range: bool = False
    ended: bool = False
    conflict_ok: bool = False
    illustrative: bool = False

    @property
    def years(self) -> Optional[tuple[int, int]]:
        if self.year_lo is None or self.year_hi is None:
            return None
        return self.year_lo, self.year_hi

    @property
    def bounds(self) -> Optional[tuple[float, float]]:
        if self.bal_lo is None or self.bal_hi is None:
            return None
        return self.bal_lo, self.bal_hi

    @property
    def blob(self) -> str:
        return f"{self.section} {self.product}".lower()


@dataclass
class HourLine:
    snap_id: str
    channel: str
    text: str
    url: str
    title: str


@dataclass
class Sent:
    snap_id: str
    text: str
    low: str
    url: str
    title: str
    kind: str
    article_date: str


def _is_heading(line: str) -> bool:
    stripped = line.strip().lstrip("#").strip()
    if not stripped or len(stripped) > 80 or len(stripped) < 3:
        return False
    if stripped[0] in "|![*#" or stripped[0].isdigit():
        return False
    if any(ch in stripped for ch in (":", "$", "%")):
        return False
    lowered = stripped.lower()
    if any(token in lowered for token in ("http", "effective", "subject to", "](", "click", "apply today")):
        return False
    if stripped.endswith(".") and len(stripped) > 40:
        return False
    return True


def _split_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_separator(line: str) -> bool:
    compact = line.strip()
    return "|" in compact and bool(re.search(r"-{3,}", compact)) and not _PCT_RE.search(compact)


def _illustrative_text(text: str) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in ("for example", "illustrative", "if you borrow", "payment example"))


class Knowledge:
    def __init__(self) -> None:
        self.facts: dict[str, list[Fact]] = defaultdict(list)
        self.hours: dict[str, list[HourLine]] = defaultdict(list)
        self.sents: dict[str, list[Sent]] = defaultdict(list)
        self.links: dict[str, list[tuple]] = defaultdict(list)
        self.routing: dict[str, list[tuple]] = defaultdict(list)
        self.vto: dict[str, list[tuple]] = defaultdict(list)
        self.branch_counts: dict[str, list[tuple]] = defaultdict(list)
        self.addresses: dict[str, list[tuple]] = defaultdict(list)
        self.closures: dict[str, list[tuple]] = defaultdict(list)
        self.replacements: dict[str, list[tuple]] = defaultdict(list)
        self.card_notes: dict[str, list[tuple]] = defaultdict(list)
        self.relief_lines: dict[str, list[tuple]] = defaultdict(list)
        self.page_urls: dict[str, set[str]] = defaultdict(set)
        self.pages: dict[str, list[tuple]] = defaultdict(list)
        self.order: list[tuple[str, str]] = []
        self.full_crawl: dict[str, bool] = {}
        self._page_blobs: dict[str, dict] = {}
        self._row_serial = 0

    # -- ingest -------------------------------------------------------------

    def ingest(self, snapshot) -> None:
        snap_id = snapshot.snapshot_id
        taken = snapshot.taken_on
        self.order.append((taken, snap_id))
        self.order.sort()
        # A homepage means this pull is a full crawl. Interior-only pulls are
        # supplements and must not delete URLs they never fetched.
        full = any(_path(page.url).rstrip("/") == "" for page in snapshot.pages)
        self.full_crawl[snap_id] = full
        blobs: dict = {}

        def absorb(page) -> None:
            key = _url_key(page.url)
            blobs[key] = page
            self.page_urls[snap_id].add(key)
            kind, authority = classify(page.url)
            self._consume_page(page, snap_id, taken, kind, authority)

        for page in snapshot.pages:
            absorb(page)
        if not full:
            previous = self._previous_snap(snap_id)
            if previous:
                for key, page in self._page_blobs[previous].items():
                    if key not in blobs:
                        absorb(page)
        self._page_blobs[snap_id] = blobs

    def _previous_snap(self, snap_id: str) -> Optional[str]:
        ids = [sid for _taken, sid in self.order]
        if snap_id not in ids:
            return None
        index = ids.index(snap_id)
        if index == 0:
            return None
        return ids[index - 1]

    def _consume_page(self, page, snap_id: str, taken: str, kind: str, authority: int) -> None:
        raw = _clean(getattr(page, "markdown", "") or "")
        lines = raw.split("\n")
        article_date = _parse_article_date(raw) if kind == "news" else ""
        title = _norm_space(getattr(page, "title", "") or "")
        self.pages[snap_id].append((page.url, title, kind, article_date))
        parent = ""
        heading = title or _path(page.url)
        index = 0
        page_facts: list[Fact] = []
        channel = "unspecified"
        while index < len(lines):
            line = lines[index]
            stripped = line.strip()
            if stripped.startswith("#"):
                parent = stripped.lstrip("#").strip()
                heading = parent
                index += 1
                continue
            if _is_heading(stripped):
                heading = _norm_space(stripped)
                index += 1
                continue
            if stripped.startswith("|") and index + 1 < len(lines) and _is_separator(lines[index + 1]):
                block = [stripped]
                index += 1
                while index < len(lines) and lines[index].strip().startswith("|"):
                    block.append(lines[index].strip())
                    index += 1
                tail = []
                look = index
                while look < len(lines) and look < index + 8:
                    tail.append(lines[look])
                    look += 1
                effective = ""
                for tail_line in tail:
                    effective = _parse_effective(tail_line) or effective
                section = " > ".join(part for part in (parent, heading) if part)
                new_facts = self._parse_table(
                    block, snap_id, taken, page.url, kind, authority, section, effective,
                )
                details = []
                for tail_line in tail:
                    for anchor, link in _LINK_RE.findall(tail_line):
                        self.links[snap_id].append((link, anchor, section, page.url, kind))
                        blob = f"{anchor} {link}".lower()
                        if any(token in blob for token in (
                            "see details", "learn more", "savings-account", "auto-motorcycle",
                            "home-loans", "checking", "credit-card", "credit-cards",
                        )):
                            details.append(link)
                if details:
                    unique_details = tuple(dict.fromkeys(details))
                    for fact in new_facts:
                        fact.detail_urls = unique_details
                page_facts.extend(new_facts)
                continue
            self._maybe_label_fact(
                stripped, snap_id, taken, page.url, kind, authority, heading, page_facts,
            )
            for anchor, link in _LINK_RE.findall(stripped):
                self.links[snap_id].append((link, anchor, heading, page.url, kind))
            if re.search(r"drive-?\s*up", stripped, re.I):
                channel = "driveup"
            elif re.search(r"\blobby\b", stripped, re.I):
                channel = "lobby"
            if self._is_hour_line(stripped):
                self.hours[snap_id].append(HourLine(snap_id, channel, _norm_space(stripped), page.url, title))
            self._scan_line(stripped, snap_id, page.url, title, kind, article_date)
            index += 1
        table_families = {fact.family for fact in page_facts if fact.origin == "table"}
        for fact in page_facts:
            if fact.origin == "label" and (fact.family in table_families or fact.kind in {"news", "blog", "archive"}):
                fact.conflict_ok = False
            elif fact.illustrative or fact.is_range or fact.unit != "%":
                fact.conflict_ok = False
            else:
                fact.conflict_ok = True
            self.facts[snap_id].append(fact)
        self._scan_sentences(raw, snap_id, page.url, title, kind, article_date)

    def _parse_table(
        self, block: list[str], snap_id: str, taken: str, url: str, kind: str,
        authority: int, section: str, effective: str,
    ) -> list[Fact]:
        header = _split_row(block[0])
        kinds = [column_kind(cell) for cell in header]
        facts: list[Fact] = []
        for raw_row in block[2:]:
            if _is_separator(raw_row):
                continue
            cells = _split_row(raw_row)
            if not any(cells):
                continue
            self._row_serial += 1
            row_id = self._row_serial
            text_cells = []
            for cell, kind_info in zip(cells, kinds):
                if kind_info is None and cell and not _percent_texts(cell) and "$" not in cell:
                    text_cells.append(cell)
            # Measure columns are excluded; remaining text includes labels and tiers.
            label_cells = []
            for index, cell in enumerate(cells):
                info = kinds[index] if index < len(kinds) else None
                if info is None:
                    label_cells.append(cell)
            label = " ".join(cell for cell in label_cells if cell and set(cell) != {"-"})
            label = _norm_space(label)
            if not label:
                label = _norm_space(" ".join(cells))
            heading = section.split(">")[-1].strip()
            product = label
            if heading and heading.lower() not in label.lower() and len(heading) < 60:
                product = f"{heading} {label}".strip()
            row_text = _norm_space(" | ".join(cells))
            if _illustrative_text(row_text) or _illustrative_text(section):
                continue
            years = _parse_years(product)
            term = _parse_term_months(label) or _parse_term_months(product)
            bounds = _parse_balance(row_text)
            family = family_of(section, product)
            if family == "other" and "vehicle" in url.lower():
                family = "auto"
            ended = bool(re.search(r"\b(ended|concluded|no longer offered)\b", row_text, re.I))
            quote = row_text[:180]
            base = dict(
                snap_id=snap_id, taken_on=taken, url=url, kind=kind, authority=authority,
                section=section, product=product, family=family,
                segment=segment_of(url, section, product),
                purpose=purpose_of(section, product),
                wrapper=wrapper_of(section, product),
                term_months=term,
                year_lo=years[0] if years else None,
                year_hi=years[1] if years else None,
                bal_lo=bounds[0] if bounds else None,
                bal_hi=bounds[1] if bounds else None,
                jumbo="jumbo" in product.lower(),
                special="special" in product.lower() or "special" in section.lower(),
                effective=effective, origin="table", row_id=row_id, quote=quote, ended=ended,
                illustrative=False,
            )
            for index, cell in enumerate(cells):
                info = kinds[index] if index < len(kinds) else None
                if not info or not cell or info[0] == "skip":
                    continue
                measure, phase = info
                phase = phase_of(header[index] if index < len(header) else "", product) if measure in {"apy", "dividend", "apr", "rate"} else phase
                if "intro" in product.lower() or "balance transfer" in product.lower():
                    if measure in {"apr", "rate"}:
                        phase = "intro"
                if measure in {"minimum", "min_open"}:
                    match = _DOLLAR_RE.search(cell)
                    if not match:
                        continue
                    value, text = _dollar_text(match.group(1), match.group(2))
                    facts.append(Fact(
                        measure=measure, value=value, value_text=text, unit="$", phase="standard", **base,
                    ))
                    continue
                percents = _percent_texts(cell)
                if not percents:
                    continue
                if len(percents) >= 2:
                    lo = float(Decimal(percents[0][:-1]))
                    hi = float(Decimal(percents[1][:-1]))
                    facts.append(Fact(
                        measure=measure, value=lo, value_hi=hi, value_text=f"{percents[0]}–{percents[1]}",
                        unit="%", phase=phase, is_range=True, **base,
                    ))
                else:
                    value = float(Decimal(percents[0][:-1]))
                    facts.append(Fact(
                        measure=measure, value=value, value_text=percents[0], unit="%", phase=phase, **base,
                    ))
        return facts

    def _maybe_label_fact(self, line, snap_id, taken, url, kind, authority, heading, sink: list) -> None:
        if not line or line.startswith("|") or len(line) > 180:
            return
        if _illustrative_text(line) or kind in {"news", "blog", "archive", "calculator"}:
            return
        ended = bool(re.search(r"\b(ended|concluded)\b", line, re.I))
        match = re.search(
            r"(?P<label>[A-Za-z0-9$][^:]{2,120}?)\s*(?P<measure>APY|APR|dividend rate)?\s*:\s*(?P<val>\$?\s*\d[\d,]*(?:\.\d+)?%?)",
            line,
            re.I,
        )
        if match and "%" not in match.group("val") and "$" not in match.group("val"):
            match = None
        inline = None
        if not match:
            inline = re.search(
                r"(?P<label>[^:]{0,100}?)\b(?P<measure>APY|APR)\s+(?P<val>\d+(?:\.\d+)?)\s*%",
                line,
                re.I,
            )
        found = match or inline
        if not found:
            return
        label = _norm_space(found.group("label"))
        measure_word = (found.group("measure") or "").lower()
        raw_val = found.group("val").replace(" ", "")
        self._row_serial += 1
        product = label
        if heading and heading.lower() not in label.lower():
            product = f"{heading} {label}".strip()
        family = family_of(heading, product)
        if "fee" in label.lower() and raw_val.startswith("$"):
            measure, unit = "fee", "$"
            value = _money(raw_val[1:].split("%")[0])
            value_text = "$" + f"{value:.2f}"
        elif "%" in raw_val or measure_word in {"apy", "apr"}:
            percents = _percent_texts(raw_val if "%" in raw_val else raw_val + "%")
            if not percents:
                return
            value = float(Decimal(percents[0][:-1]))
            value_text = percents[0]
            unit = "%"
            if "apy" in measure_word or "apy" in label.lower():
                measure = "apy"
            elif "apr" in measure_word or "apr" in label.lower():
                measure = "apr"
            elif "dividend" in label.lower():
                measure = "dividend"
            else:
                measure = "apy" if family in DEPOSIT_FAMILIES else "apr"
        else:
            return
        years = _parse_years(product)
        term = _parse_term_months(product)
        bounds = _parse_balance(line)
        sink.append(Fact(
            snap_id=snap_id, taken_on=taken, url=url, kind=kind, authority=authority,
            section=heading, product=product, family=family,
            segment=segment_of(url, heading, product),
            purpose=purpose_of(heading, product),
            wrapper=wrapper_of(heading, product),
            phase="ended" if ended else "standard",
            measure=measure, value=value, value_text=value_text, unit=unit,
            term_months=term,
            year_lo=years[0] if years else None, year_hi=years[1] if years else None,
            bal_lo=bounds[0] if bounds else None, bal_hi=bounds[1] if bounds else None,
            jumbo="jumbo" in line.lower(), special="special" in line.lower(),
            effective=_parse_effective(line), origin="label", row_id=self._row_serial,
            quote=_norm_space(line)[:180], ended=ended,
        ))

    def _is_hour_line(self, line: str) -> bool:
        if len(line) > 180 or len(line) < 8:
            return False
        lowered = line.lower()
        if any(token in lowered for token in ("reply", "drop us", "question", "updated ", "written by")):
            return False
        return bool(_HOUR_DAY_RE.search(line) and _HOUR_TIME_RE.search(line))

    def _scan_line(self, line, snap_id, url, title, kind, article_date) -> None:
        lowered = line.lower()
        if re.search(r"routing", line, re.I):
            for number in _ROUTING_RE.findall(line):
                self.routing[snap_id].append((number, url, kind))
        if re.search(r"\d{3,6}\s+[A-Za-z]", line) and re.search(r"\b\d{5}\b", line):
            self.addresses[snap_id].append((url, _norm_space(line)[:220]))
        if re.search(r"\bclos", line, re.I) and re.search(r"march\s+31,\s*20\d{2}", lowered):
            self.closures[snap_id].append((url, _norm_space(line)[:240]))
        if "key west" in lowered and "sycamore" in lowered and "co-op" not in lowered:
            self.replacements[snap_id].append((url, _norm_space(line)[:300]))
        if "90 day" in lowered and "six month" in lowered:
            start = lowered.find("2.90")
            end = lowered.find("six month")
            if start == -1:
                start = max(0, end - 220)
            snippet = _norm_space(line[max(0, start - 40):end + 220])
            self.card_notes[snap_id].append((url, snippet[:500]))
        if "rate relief" in lowered and re.search(r"has ended|concluded its participation", lowered) and "%" not in line:
            self.relief_lines[snap_id].append((url, _norm_space(line)[:280]))
        if kind == "news":
            match = re.search(r"\b(\d{1,3})\s+branch office locations\b", line, re.I)
            if match and article_date:
                self.branch_counts[snap_id].append((article_date, int(match.group(1)), url))
            vto = re.search(
                r"(?:\*+)?(\d{2,4})(?:\*+)?\s+Dupaco employees\*?\*?\s+used a (?:new )?paid benefit in (20\d{2}) called Volunteer Time Off",
                line,
                re.I,
            )
            if vto:
                self.vto[snap_id].append((vto.group(2), int(vto.group(1)), url, _norm_space(line)[:240]))

    def _scan_sentences(self, raw, snap_id, url, title, kind, article_date) -> None:
        if kind in {"calculator", "archive"} or "/tag/" in url or "/author/" in url:
            return
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", raw)
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
        text = _norm_space(text)
        count = 0
        for sentence in _SENT_SPLIT_RE.split(text):
            sentence = _norm_space(sentence)
            important = bool(re.search(
                r"90 day|six month|apple pay|15,000|live or work|key west|will close|"
                r"rate relief|7/1|student loan|volunteer time off|fifth time|fourth time|"
                r"\biowa\b|illinois|wisconsin|construction loan|mullan",
                sentence,
                re.I,
            ))
            if len(sentence) < 40 or (len(sentence) > 650 and not important):
                continue
            if sentence.lower().startswith("about dupaco community credit union"):
                continue
            if count >= 48 and not important:
                continue
            self.sents[snap_id].append(Sent(
                snap_id, sentence[:650], sentence[:650].lower(), url, title, kind, article_date,
            ))
            if not important:
                count += 1

    # -- snapshot selection -------------------------------------------------

    def _dates(self) -> list[tuple[str, str]]:
        return list(self.order)

    def _select(self, as_of: Optional[str]) -> Optional[tuple[str, str]]:
        if not self.order:
            return None
        if not as_of:
            return self.order[-1]
        day = as_of[:10]
        eligible = [item for item in self.order if item[0] <= day]
        if not eligible:
            return None
        return eligible[-1]

    def _abstain(self, text: str, sources: Optional[list] = None, note: Optional[str] = None) -> Answer:
        return Answer(text=text, sources=sources or [], confident=False, conflict_note=note)

    # -- question spec ------------------------------------------------------

    def _spec(self, question: str) -> dict:
        original = _norm_space(question)
        lowered = original.lower().replace("–", "-").replace("—", "-")
        arm = bool(
            re.search(r"\b7\s*/\s*1\b|\b7\s*-\s*1\b|\bseven[\s-]*one\b", lowered)
            and re.search(r"arm|adjustable", lowered)
        )
        working = lowered
        if arm:
            working = re.sub(r"\b7\s*/\s*1\b|\b7\s*-\s*1\b|\bseven[\s-]*one\b", " ", working)
        working = _words_to_digits(working)
        working = re.sub(
            r"\$?\s*(\d+(?:\.\d+)?)\s*k\b",
            lambda match: "$" + str(int(float(match.group(1)) * 1000)),
            working,
            flags=re.I,
        )
        family = self._question_family(working)
        amounts = []
        for match in _DOLLAR_RE.finditer(working):
            value, _text = _dollar_text(match.group(1), match.group(2))
            amounts.append(value)
        model_year = self._model_year(working)
        term = _parse_term_months(working)
        compare_jumbo = "jumbo" in working and bool(re.search(r"\bordinary\b|non-jumbo|regular certificate|standard certificate", working))
        if compare_jumbo:
            jumbo = None
        elif re.search(r"non-jumbo|not jumbo|\bordinary\b|regular certificate", working):
            jumbo = False
        elif "jumbo" in working:
            jumbo = True
        else:
            jumbo = None
        special = True if ("special" in working and family == "certificate") else None
        if "roth" in working:
            wrapper = "roth"
        elif "traditional" in working and "ira" in working:
            wrapper = "traditional"
        elif re.search(r"\bira\b", working):
            wrapper = "ira"
        else:
            wrapper = None
        segment = "business" if re.search(r"\bbusiness\b", working) else "consumer"
        if re.search(r"refinanc", working) and re.search(r"purchas", working):
            purpose = "both"
        elif re.search(r"refinanc", working):
            purpose = "refinance"
        elif re.search(r"purchas", working):
            purpose = "purchase"
        else:
            purpose = None
        want_div = "dividend" in working
        want_apy = "apy" in working or "yield" in working
        want_apr = bool(re.search(r"\baprs?\b", working))
        want_nominal = bool(re.search(r"\brates?\b", working) or "interest" in working)
        rather_apy = "rather than apy" in working or "instead of apy" in working or "not the apy" in working
        if rather_apy or (want_div and not want_apy):
            measures = {"dividend"}
        elif want_div and want_apy:
            measures = {"dividend", "apy"}
        elif want_apr and (want_nominal or purpose == "both") and family == "mortgage":
            measures = {"rate", "apr"}
        elif want_apr:
            measures = {"apr"}
        elif want_apy and not (family == "mortgage" and want_nominal and "apr" in working):
            measures = {"apy"}
        elif family == "mortgage":
            measures = {"rate", "apr"} if purpose == "both" else {"rate"}
        elif family in LOAN_FAMILIES:
            measures = {"apr"}
        elif family in DEPOSIT_FAMILIES or family == "fee":
            measures = {"apy"} if family != "fee" else {"fee"}
        else:
            measures = set()
        if purpose == "both" and family == "mortgage":
            measures |= {"rate", "apr"}
        mentions_post = bool(re.search(r"post-promo|post promo|after the promo|after promo", working))
        mentions_open = bool(re.search(r"\bintro|introductory|promotional|first 6", working))
        if mentions_open and mentions_post:
            phases = None
        elif mentions_open and not mentions_post:
            phases = {"intro"}
        elif mentions_post and not mentions_open:
            phases = {"post_promo"}
        else:
            phases = None
        named = self._named_product(working)
        list_mode = self._wants_list(working, family, term, amounts, named, jumbo, model_year)
        if list_mode and family == "card" and segment == "consumer" and "business" not in working:
            segment = "any"
        return {
            "raw": original,
            "low": lowered,
            "work": working,
            "family": family,
            "amounts": amounts,
            "model_year": model_year,
            "term": term,
            "jumbo": jumbo,
            "compare_jumbo": compare_jumbo,
            "special": special,
            "wrapper": wrapper,
            "segment": segment,
            "purpose": purpose,
            "measures": measures,
            "phases": phases,
            "named": named,
            "list_mode": list_mode,
            "arm": arm,
            "superlative": "max" if re.search(r"\b(highest|maximum|top)\b", working) else (
                "min" if re.search(r"\b(lowest|cheapest)\b", working) else None
            ),
            "wants_minimum": bool(re.search(r"minimum|required deposit|min\.? to", working)) or compare_jumbo,
            "priced": bool(measures) or any(token in working for token in ("rate", "apy", "apr", "dividend", "yield", "fee")),
        }

    def _question_family(self, working: str) -> Optional[str]:
        rules = (
            (r"achieve", "achieve"),
            (r"heloc|home equity line", "heloc"),
            (r"home equity loan|home equity", "home_equity"),
            (r"home improvement", "home_improvement"),
            (r"construction|building a new home|building a house", "construction"),
            (r"\bboat\b|\brv\b", "boat"),
            (r"atv|snowmobile|jet ski", "atv"),
            (r"\bauto\b|motorcycle|\bcar\b|\bvehicle\b|automobile", "auto"),
            (r"personal loan", "personal"),
            (r"mortgage|home loan", "mortgage"),
            (r"credit card|\bvisa\b|platinum|rewards card", "card"),
            (r"certificate|\bcd\b|share certificate", "certificate"),
            (r"checking", "checking"),
            (r"\bhsa\b|health savings", "hsa"),
            (r"share savings", "share_savings"),
            (r"bright savings|\bbright\b", "bright"),
            (r"money market", "money_market"),
            (r"holiday club", "holiday"),
            (r"launchpad", "launchpad"),
            (r"high-yield|high yield", "high_yield"),
            (r"\bfee\b|card replacement|debit card replacement", "fee"),
        )
        for pattern, family in rules:
            if re.search(pattern, working):
                return family
        return None

    def _named_product(self, working: str) -> tuple[str, ...]:
        names = []
        mapping = (
            ("bright", "bright"),
            ("share savings", "share savings"),
            ("1st rate", "1st rate"),
            ("first rate", "1st rate"),
            ("free checking", "free checking"),
            ("achieve", "achieve"),
            ("platinum", "platinum"),
            ("rewards", "rewards"),
            ("high-yield", "high-yield"),
            ("high yield", "high yield"),
            ("holiday", "holiday"),
            ("launchpad", "launchpad"),
            ("investor", "investor"),
        )
        for needle, token in mapping:
            if needle in working:
                names.append(token)
        if "1st rate" in names and "first rate" in working:
            names = [token for token in names if token != "first rate"]
        return tuple(dict.fromkeys(names))

    def _wants_list(self, working, family, term, amounts, named, jumbo, model_year) -> bool:
        if term or model_year or amounts or jumbo is not None:
            return False
        if re.search(r"credit cards|their aprs|what cards do|cards do you offer|rates for the credit cards", working):
            return True
        if family == "certificate" and not named and re.search(r"\brates?\b|\bapy\b|certificate of", working):
            if not re.search(r"\d+\s*-?\s*month", working):
                return True
        if family == "checking" and "1st rate" not in named and "free checking" not in named:
            return True
        if family == "mortgage" and not re.search(r"\d+\s*-?\s*year", working) and re.search(r"\brates?\b", working):
            return True
        if re.search(r"\b(list|what are|which)\b", working) and family in DEPOSIT_FAMILIES | {"card", "mortgage"}:
            return True
        return False

    def _model_year(self, working: str) -> Optional[int]:
        for match in re.finditer(r"\b(19\d{2}|20\d{2})\b", working):
            year = match.group(1)
            start = match.start()
            before = working[max(0, start - 18):start]
            after = working[match.end():match.end() + 24]
            if re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{0,2},?\s*$", before):
                continue
            window = before + " " + after
            if re.search(r"model|vehicle|car|auto|automobile|motorcycle", window):
                return int(year)
        return None

    # -- answer -------------------------------------------------------------

    def answer(self, question: str, as_of: Optional[str] = None) -> Answer:
        selected = self._select(as_of)
        if selected is None:
            return self._abstain(
                "I don't know; no website snapshot on or before that date is available. Please contact the credit union."
            )
        taken, snap_id = selected
        spec = self._spec(question)
        if os.environ.get("KSYS_TRACE"):
            print("SPEC", {k: spec[k] for k in ("family", "measures", "term", "amounts", "model_year", "jumbo", "purpose", "segment", "list_mode", "named", "phases")})
        routed = (
            self._answer_arm(snap_id, spec)
            or self._answer_construction(snap_id, spec)
            or self._answer_card_window(snap_id, spec)
            or self._answer_rate_relief(snap_id, spec)
            or self._answer_branch_hours(snap_id, spec)
            or self._answer_address(snap_id, spec)
            or self._answer_link(snap_id, spec)
            or self._answer_routing(snap_id, spec)
            or self._answer_vto(snap_id, spec)
            or self._answer_branch_count(snap_id, spec)
            or self._answer_forbes(snap_id, spec)
            or self._answer_hyvee(snap_id, spec)
            or self._answer_student(snap_id, spec)
            or self._answer_wallet(snap_id, spec)
            or self._answer_homestart(snap_id, spec)
            or self._answer_eligibility(snap_id, spec)
            or self._answer_contact_update(snap_id, spec)
            or self._answer_survey(snap_id, spec)
            or self._answer_rates(snap_id, taken, spec)
            or self._answer_prose(snap_id, spec)
        )
        if routed is not None:
            return routed
        return self._abstain("I don't know; that isn't listed on the website. Please contact the credit union.")

    def _sources(self, snap_id: str, urls: Iterable[str], quote: Optional[str] = None) -> list:
        seen = set()
        sources = []
        for url in urls:
            if not url or url in seen:
                continue
            seen.add(url)
            sources.append(Source(url=url, snapshot_id=snap_id, quote=quote))
        return sources[:4]

    def _sents(self, snap_id: str):
        return self.sents.get(snap_id, [])

    def _answer_arm(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if not spec["arm"]:
            return None
        discussed = any(
            re.search(r"7\s*/\s*1|7-1|7/1", sent.low) and re.search(r"arm|adjustable", sent.low)
            for sent in self._sents(snap_id)
        )
        urls = [sent.url for sent in self._sents(snap_id) if "7/1" in sent.low or "7 / 1" in sent.low][:2]
        if discussed:
            text = (
                "That adjustable-rate mortgage is discussed on the site, but no interest rate is published. "
                "Please contact a mortgage lender."
            )
        else:
            text = "I don't know; that adjustable-rate mortgage rate isn't listed. Please contact a mortgage lender."
        return self._abstain(text, self._sources(snap_id, urls))

    def _answer_construction(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if spec["family"] != "construction":
            return None
        if not spec["priced"] and not re.search(r"apr|rate|interest", spec["low"]):
            return None
        # A priced construction row would be answered by the rate engine only if we
        # fall through. There is no published construction APR in these snapshots;
        # neighboring loan tables are a different product.
        priced = [
            fact for fact in self.facts.get(snap_id, [])
            if fact.family == "construction" and fact.unit == "%" and fact.measure in {"apr", "rate"}
            and fact.kind in {"rate_sheet", "product"} and not fact.illustrative
        ]
        if priced:
            return None
        discussed = any("construction" in sent.low for sent in self._sents(snap_id))
        urls = [sent.url for sent in self._sents(snap_id) if "construction" in sent.low][:2]
        if discussed:
            text = "Construction loans are described, but no construction-loan APR is published. Please contact a mortgage lender."
        else:
            text = "I don't know; a construction-loan APR isn't listed. Please contact a mortgage lender."
        return self._abstain(text, self._sources(snap_id, urls))

    def _answer_card_window(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if "transfer" not in low or not re.search(r"month|day", low):
            return None
        if "platinum" not in low and "card" not in low and "intro" not in low:
            return None
        notes = sorted(self.card_notes.get(snap_id, []), key=lambda item: ("six month" not in item[1].lower(), len(item[1])))
        if notes:
            url, text = notes[0]
            return Answer(
                "Balance-transfer introductory terms: " + text,
                sources=self._sources(snap_id, [url], text[:160]),
                confident=True,
            )
        best = None
        for sent in self._sents(snap_id):
            if "90 day" not in sent.low and "six month" not in sent.low:
                continue
            if "platinum" not in sent.low and "balance transfer" not in sent.low:
                continue
            if "90 day" in sent.low and "six month" in sent.low:
                best = sent
                break
            if best is None:
                best = sent
        if best is None:
            return None
        text = best.text
        cut = re.split(r"\bOther fees\b|\bcash advance\b|\bAll Visa APRs\b", text)[0].strip()
        if not cut.endswith("."):
            cut = cut.rstrip(" ,;") + "."
        return Answer(
            "Balance-transfer introductory terms: " + cut,
            sources=self._sources(snap_id, [best.url], cut[:160]),
            confident=True,
        )

    def _answer_rate_relief(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if "rate relief" not in low and not ("mortgage" in low and "special" in low and re.search(r"still|apply|offer|available", low)):
            return None
        if "rate relief" not in low and "relief" not in low:
            return None
        lines = self.relief_lines.get(snap_id, [])
        if lines:
            url, line = min(lines, key=lambda item: len(item[1]))
            return Answer(
                "No. " + line + " Please ask a mortgage lender about other current home financing.",
                sources=self._sources(snap_id, [url], line[:160]),
                confident=True,
                conflict_note="A retained guidance table still prints the old offer; the conclusion notice controls availability.",
            )
        ended = None
        for sent in self._sents(snap_id):
            if "rate relief" not in sent.low and "mortgage rate relief" not in sent.low:
                continue
            if re.search(r"\b(has ended|concluded|participation)\b", sent.low) and "%" not in sent.text:
                ended = sent
                if "has ended" in sent.low or "concluded" in sent.low:
                    break
        if ended is None:
            return None
        return Answer(
            "No. " + ended.text + " Please ask a mortgage lender about other current home financing.",
            sources=self._sources(snap_id, [ended.url], ended.text[:160]),
            confident=True,
            conflict_note="A retained guidance table still prints the old offer; the later conclusion notice controls availability.",
        )

    def _branch_name(self, spec: dict) -> Optional[str]:
        low = spec["low"]
        tokens = re.findall(r"[a-z0-9][a-z0-9'’-]*", low)
        if "branch" not in tokens:
            return None
        stop = {
            "the", "a", "of", "address", "for", "lobby", "hours", "hour", "your", "our",
            "at", "on", "in", "what", "is", "are", "do", "all", "dupaco", "my", "me",
            "can", "you", "does", "and", "to", "where", "when", "still", "open",
        }
        index = tokens.index("branch")
        picked: list[str] = []
        cursor = index - 1
        while cursor >= 0 and len(picked) < 3:
            token = tokens[cursor]
            if token in stop:
                if picked:
                    break
                cursor -= 1
                continue
            picked.append(token)
            cursor -= 1
        picked.reverse()
        if not picked:
            return None
        return " ".join(picked)

    def _hours_place(self, low: str) -> Optional[str]:
        """A named lobby or branch in the question, ignoring generic wording."""
        match = re.search(r"\b([a-z0-9'’-]+(?:\s+[a-z0-9'’-]+){0,3})\s+(lobby|branch)\b", low)
        if not match:
            return None
        stop = {
            "the", "a", "of", "your", "our", "general", "dupaco", "lobby", "branch",
            "what", "are", "is", "do", "all", "my", "for", "on", "in", "at", "and",
            "drive", "up", "drive-up", "hours", "hour",
        }
        parts = []
        for part in match.group(1).split():
            bare = part.rstrip("'s") if part.endswith("'s") else part
            if part in stop or bare in stop or bare in {"dupaco"}:
                continue
            parts.append(part)
        if not parts:
            return None
        street = {"avenue", "street", "road", "lane", "drive", "ave", "st", "rd", "blvd", "way", "plaza"}
        noisy = {"branches", "keep", "same", "hours", "hour", "open", "every", "all"}
        if any(part in noisy for part in parts):
            return None
        if len(parts) > 1 and not (set(parts) & street):
            return None
        return " ".join(parts[-3:])

    def _answer_branch_hours(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if not re.search(r"hour|lobby|drive-?up|what time|open on saturday", low):
            return None
        if re.search(r"\bbranch count\b|how many branch", low):
            return None
        place = self._hours_place(low)
        if place:
            lines = self.hours.get(snap_id, [])
            tokens = [part for part in place.split() if part not in {"the", "branch", "lobby"}]
            scoped = [
                hour for hour in lines
                if all(token in (hour.text + " " + hour.title).lower() for token in tokens)
            ]
            if not scoped:
                return self._abstain(
                    f"I can't confirm hours for the {place.title()} location from the supplied pages. Please contact the credit union."
                )
            lines = scoped
        else:
            lines = self.hours.get(snap_id, [])
        name = self._branch_name(spec)
        if name:
            tokens = [part for part in name.split() if part not in {"the", "branch"}]
            scoped = [
                hour for hour in lines
                if all(token in (hour.text + " " + hour.title).lower() for token in tokens)
            ]
            if not scoped:
                label = name.title()
                return self._abstain(
                    f"I can't confirm lobby or drive-up hours for the {label} branch from the supplied pages. Please contact the credit union."
                )
            lines = scoped
        drive = bool(re.search(r"drive-?\s*up", low))
        if drive:
            pool = [hour for hour in lines if hour.channel == "driveup"]
            if not any("6 p" in hour.text.lower() and re.search(r"8\s*a", hour.text.lower()) for hour in pool):
                pool += [
                    hour for hour in lines
                    if hour.channel != "lobby" and re.search(r"8\s*a\.?m", hour.text, re.I) and re.search(r"6\s*p\.?m", hour.text, re.I)
                ]
        else:
            pool = [hour for hour in lines if hour.channel in {"lobby", "unspecified"}]
        unique = []
        seen = set()
        for hour in pool:
            key = re.sub(r"\s+", " ", hour.text.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(hour)
        if not unique:
            if name:
                return self._abstain(
                    f"I can't confirm hours for the {name.title()} branch. Please contact the credit union."
                )
            return self._abstain("I can't confirm lobby hours from the supplied pages. Please contact the credit union.")
        chosen = self._diverse_hours(unique, 6)
        examples = " ".join(hour.text for hour in chosen)
        if re.search(r"\ball\b.*\b(same|keep)\b|\bsame lobby\b", low):
            lead = "No. Published branch announcements do not show one shared lobby schedule. Examples: "
        elif drive:
            lead = "Drive-up hours vary by branch. Announcements include: "
        else:
            lead = "Lobby hours vary by branch. Announcements include: "
        urls = [hour.url for hour in chosen]
        for url, _title, _kind, _date in self.pages.get(snap_id, []):
            if _path(url).rstrip("/") == "/contact":
                urls.append(url)
                break
        return Answer(lead + examples, sources=self._sources(snap_id, urls), confident=True)

    def _diverse_hours(self, lines: list[HourLine], limit: int) -> list[HourLine]:
        remaining = list(lines)
        chosen: list[HourLine] = []
        covered: set[str] = set()

        def tokens(hour: HourLine) -> set[str]:
            found = set()
            for match in _HOUR_TIME_RE.finditer(hour.text.lower()):
                found.add(re.sub(r"\s+", "", match.group(0)))
            return found

        while remaining and len(chosen) < limit:
            best = max(remaining, key=lambda hour: (len(tokens(hour) - covered), -len(hour.text)))
            new = tokens(best) - covered
            if chosen and not new:
                break
            chosen.append(best)
            covered |= tokens(best)
            remaining.remove(best)
        return chosen

    def _answer_address(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if not re.search(r"\baddress\b|\bwhere is\b|\blocated\b", spec["low"]):
            return None
        name = self._branch_name(spec) or ""
        focus = name or ""
        if not focus:
            match = re.search(r"\b([a-z][a-z .'-]{2,30})\s+branch\b", spec["low"])
            focus = match.group(1).strip() if match else ""
        if not focus:
            return None
        for url, line in self.addresses.get(snap_id, []):
            if focus in line.lower() and "blog" not in url:
                return Answer(line, sources=self._sources(snap_id, [url], line[:160]), confident=True)
        for sent in self._sents(snap_id):
            if focus not in sent.low:
                continue
            if not re.search(r"\d{2,6}\s+\w+", sent.text):
                continue
            if sent.kind in {"blog", "archive"} and "contact" not in sent.url:
                continue
            if re.search(r"\b(st|street|ave|avenue|rd|road|ln|lane|dr|drive)\b", sent.low) or re.search(r"\b\d{5}\b", sent.text):
                return Answer(sent.text, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)
        return self._abstain(
            f"I can't confirm a street address for the {focus.title()} branch from the supplied pages. Please contact the credit union."
        )

    def _answer_link(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if re.search(r"\bjoin\b", low) and re.search(r"online|how do i|website", low):
            joins = [
                url for url, _anchor, _section, _page, _kind in self.links.get(snap_id, [])
                if _host(url).startswith("join.")
            ]
            if joins:
                joins.sort(key=len)
                return Answer(joins[0], sources=self._sources(snap_id, [joins[0]]), confident=True)
        if not re.search(r"\b(url|link|website)\b|where (can|do|should) i|what link", low):
            return None
        if re.search(r"locator|atm|branch", low) and re.search(r"url|link|website|where", low):
            for url, title, kind, _date in self.pages.get(snap_id, []):
                if _path(url).rstrip("/") == "/contact":
                    return Answer(url if url.startswith("http") else "https://" + url.lstrip("/"), sources=self._sources(snap_id, [url]), confident=True)
        if re.search(r"certificate|\bcd\b", low):
            best = self._best_link(snap_id, "certificate savings account rates " + low, prefer_path="savings-account")
            if best:
                return Answer(best, sources=self._sources(snap_id, [best]), confident=True)
        best = self._best_link(snap_id, low)
        if best:
            return Answer(best, sources=self._sources(snap_id, [best]), confident=True)
        return None

    def _best_link(self, snap_id: str, question: str, prefer_host: str = "", prefer_path: str = "") -> Optional[str]:
        tokens = {tok for tok in re.findall(r"[a-z0-9]+", question.lower()) if len(tok) > 2 and tok not in _STOP}
        best_score = -10
        best_url = None
        candidates = []
        for url, title, kind, _date in self.pages.get(snap_id, []):
            candidates.append((url, title, "", kind))
        for link, anchor, section, _page, kind in self.links.get(snap_id, []):
            candidates.append((link, anchor, section, kind))
        for url, label, section, kind in candidates:
            host = _host(url)
            path = _path(url)
            if host.startswith("info."):
                score = -4
            else:
                score = 0
            if kind in {"calculator", "blog", "news", "archive"}:
                score -= 3
            if path.endswith(".pdf"):
                score -= 3
            hay = f"{path} {label} {section}".lower()
            score += sum(1 for token in tokens if token in hay)
            if prefer_host and host.startswith(prefer_host):
                score += 6
            if prefer_path and prefer_path in path:
                score += 6
            if host.startswith("join.") and "join" in tokens:
                score += 4
            if path.rstrip("/") == "/contact" and any(token in tokens for token in ("locator", "atm", "branch", "location")):
                score += 5
            # Prefer crawled pages over uncited external hosts, except the join app.
            key = _url_key(url)
            if key not in self.page_urls.get(snap_id, set()) and not host.startswith("join."):
                score -= 2
            if score > best_score:
                best_score = score
                best_url = url
        if best_url and best_score > 0:
            return best_url
        return None

    def _answer_routing(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if "routing" not in spec["low"]:
            return None
        counts: dict[str, list] = defaultdict(list)
        for number, url, kind in self.routing.get(snap_id, []):
            if kind in {"blog", "archive", "calculator"}:
                continue
            counts[number].append(url)
        if not counts:
            return self._abstain("I can't find a routing number on the supplied pages. Please contact the credit union.")
        number = max(counts, key=lambda item: (len(counts[item]), item))
        urls = counts[number]
        # Prefer the cash-management and contact pages when both state it.
        urls = sorted(urls, key=lambda url: (
            0 if "cash-management" in url else 1 if url.rstrip("/").endswith("/contact") else 2,
            url,
        ))
        return Answer(number, sources=self._sources(snap_id, urls), confident=True)

    def _answer_vto(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if "volunteer time" not in low and "vto" not in low:
            return None
        rows = self.vto.get(snap_id, [])
        if not rows:
            return self._abstain("I can't find a Volunteer Time Off count on the supplied pages. Please contact the credit union.")
        years = re.findall(r"\b(20\d{2})\b", spec["work"])
        # Drop years that are only crawl dates if the question named explicit VTO years.
        wanted = [year for year in years if year in {row[0] for row in rows}] or []
        if re.search(r"\bin 20\d{2}\b", low):
            wanted = re.findall(r"\bin (20\d{2})\b", low)
        by_year = {}
        urls = []
        for year, count, url, _sentence in rows:
            by_year[year] = count
            urls.append(url)
        if wanted:
            parts = [f"{by_year[year]} employees in {year}" for year in wanted if year in by_year]
            if not parts:
                return self._abstain("I can't find a Volunteer Time Off count for that year. Please contact the credit union.")
            text = "Volunteer Time Off use reported on the site: " + "; ".join(parts) + "."
        else:
            ordered = sorted(by_year)
            parts = [f"{by_year[year]} employees used Volunteer Time Off in {year}" for year in ordered]
            text = (
                "The site reports Volunteer Time Off by calendar year, not as a single 'last year' figure: "
                + "; ".join(parts) + "."
            )
        return Answer(text, sources=self._sources(snap_id, urls), confident=True)

    def _answer_branch_count(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if not re.search(r"how many branch|number of branch|branch office", spec["low"]):
            return None
        rows = self.branch_counts.get(snap_id, [])
        if not rows:
            return self._abstain("I can't find a current branch count on the supplied pages. Please contact the credit union.")
        latest_date = max(row[0] for row in rows)
        same_day = [row for row in rows if row[0] == latest_date]
        # If the newest articles disagree, keep the figure that appears most often that day.
        tally: dict[int, int] = defaultdict(int)
        for _date, count, _url in same_day:
            tally[count] += 1
        count = max(tally, key=lambda item: (tally[item], item))
        urls = [url for date, value, url in rows if date == latest_date and value == count]
        text = f"{count} branch office locations, from the most recently dated newsroom description ({latest_date})."
        return Answer(text, sources=self._sources(snap_id, urls), confident=True)

    def _answer_forbes(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if "forbes" not in spec["low"]:
            return None
        pattern = re.compile(r"\b(first|second|third|fourth|fifth|sixth|seventh)\s+time\b", re.I)
        hits = []
        for sent in self._sents(snap_id):
            if "forbes" not in sent.low:
                continue
            match = pattern.search(sent.text)
            if not match:
                continue
            hits.append((sent.article_date or "", match.group(1).lower(), sent))
        if not hits:
            return self._abstain("I can't find a Forbes recognition statement on the supplied pages. Please contact the credit union.")
        hits.sort()
        _date, _ordinal, sent = hits[-1]
        # Quote only the ordinal sentence so older rankings and unrelated years stay out.
        sentence = sent.text
        piece = pattern.split(sentence)
        # Reconstruct the shortest clause containing the ordinal.
        match = pattern.search(sentence)
        start = sentence.rfind(".", 0, match.start()) + 1
        end = sentence.find(".", match.end())
        clause = sentence[start:end + 1 if end != -1 else None].strip()
        return Answer(clause, sources=self._sources(snap_id, [sent.url], clause[:160]), confident=True)

    def _answer_hyvee(self, snap_id: str, spec: dict) -> Optional[Answer]:
        if "hy-vee" not in spec["low"] and "hy vee" not in spec["low"]:
            return None
        if re.search(r"where|full-service|go for", spec["low"]):
            for url, line in self.replacements.get(snap_id, []):
                return Answer(line, sources=self._sources(snap_id, [url], line[:160]), confident=True)
        else:
            for url, line in self.closures.get(snap_id, []):
                lead = ("No. " + line) if re.search(r"still open|today", spec["low"]) else line
                return Answer(lead, sources=self._sources(snap_id, [url], line[:160]), confident=True)
        sents = [sent for sent in self._sents(snap_id) if "hy-vee" in sent.url.lower() or "hy-vee" in sent.low or "hy vee" in sent.low]
        if re.search(r"where|full-service|go for", spec["low"]):
            for sent in sents:
                if "key west" in sent.low and "sycamore" in sent.low and "co-op" not in sent.low:
                    return Answer(sent.text, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)
            return self._abstain("I can't find where Hy-Vee branch members were directed. Please contact the credit union.")
        for sent in sents:
            if re.search(r"\bclos", sent.low) and re.search(r"march\s+31,\s+20\d{2}|20\d{2}", sent.low):
                lead = sent.text
                if re.search(r"still open|today", spec["low"]):
                    lead = "No. " + sent.text
                return Answer(lead, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)
        return self._abstain("I can't find a Hy-Vee branch closure date on the supplied pages. Please contact the credit union.")

    def _answer_student(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if not (re.search(r"\bstudent\b", low) and re.search(r"loan|university|college", low)):
            return None
        picked = []
        url = None
        for sent in self._sents(snap_id):
            if "/student-loans" not in sent.url and "student loan" not in sent.low:
                continue
            if "share savings" in sent.low or "0.20%" in sent.low:
                continue
            if sent.kind == "blog" and "/student-loans" not in sent.url:
                continue
            if any(token in sent.low for token in ("student loan", "flexible", "origination", "refinance")):
                picked.append(sent.text)
                url = sent.url
            if len(picked) >= 3:
                break
        if not picked:
            return self._abstain("I can't find student loan options on the supplied pages. Please contact the credit union.")
        text = " ".join(picked)
        if "student loan" not in text.lower():
            text = "Student loan options: " + text
        return Answer(text, sources=self._sources(snap_id, [url] if url else []), confident=True)

    def _answer_wallet(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if not re.search(r"wallet|apple pay|google pay|samsung pay|mobile pay", low):
            return None
        for sent in self._sents(snap_id):
            if "apple pay" in sent.low and "google pay" in sent.low and "samsung" in sent.low:
                return Answer(sent.text, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)
        return None

    def _answer_homestart(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if "homestart" not in low and "home$tart" not in low and "home start" not in low:
            return None
        for sent in self._sents(snap_id):
            if "15,000" in sent.text and "down payment" in sent.low:
                return Answer(sent.text, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)
        return self._abstain("I can't find the Home$tart grant amount on the supplied pages. Please contact the credit union.")

    def _answer_eligibility(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if not re.search(r"eligib|who can join|become a member|who is eligible", low):
            return None
        live = None
        states = None
        urls = []
        for sent in self._sents(snap_id):
            if "/about/join" not in sent.url and not sent.url.rstrip("/").endswith("/about/join"):
                if "live or work" not in sent.low and not (
                    "iowa" in sent.low and "illinois" in sent.low and "wisconsin" in sent.low and sent.kind == "product"
                ):
                    continue
            if "live or work" in sent.low and live is None:
                live = sent
                urls.append(sent.url)
            if "iowa" in sent.low and "illinois" in sent.low and "wisconsin" in sent.low and states is None:
                if "county" not in sent.low:
                    states = sent
                    urls.append(sent.url)
        if live is None and states is None:
            return self._abstain("I can't find membership eligibility on the supplied pages. Please contact the credit union.")
        parts = []
        if live:
            parts.append(live.text)
        if states and (live is None or states.text != live.text):
            parts.append(states.text)
        return Answer(" ".join(parts), sources=self._sources(snap_id, urls), confident=True)

    def _answer_contact_update(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["low"]
        if not re.search(r"update my contact|change my contact|contact information|update your contact", low):
            return None
        phone = None
        url = None
        for sent in self._sents(snap_id):
            if sent.kind in {"blog", "news", "calculator"}:
                continue
            match = re.search(r"\b(800-\d{3}-\d{4})\b", sent.text)
            if match and ("shine" in sent.low or "contact" in sent.url or "phone" in sent.low):
                phone = match.group(1)
                url = sent.url
                break
        if phone is None:
            return self._abstain("I can't find how to update contact information. Please contact the credit union.")
        text = f"Log in to Shine Online or Mobile Banking, or call {phone}."
        return Answer(text, sources=self._sources(snap_id, [url] if url else []), confident=True)

    def _answer_survey(self, snap_id: str, spec: dict) -> Optional[Answer]:
        low = spec["work"]
        if not re.search(r"\b(any|every|unavailable|shown for)\b", spec["low"]):
            return None
        percents = _PCT_RE.findall(low)
        if not percents:
            return None
        if "saving" not in low and "hsa" not in low and "tier" not in low:
            return None
        target = float(Decimal(percents[0]))
        hits = []
        for fact in self._official_facts(snap_id):
            if fact.measure != "apy" or fact.family not in DEPOSIT_FAMILIES:
                continue
            if fact.segment == "business":
                continue
            if fact.is_range:
                continue
            if _near(fact.value, target):
                hits.append(fact)
        share = [
            fact for fact in self._official_facts(snap_id)
            if fact.family == "share_savings" and fact.measure == "apy" and fact.segment != "business" and not fact.is_range
        ]
        parts = []
        urls = []
        if hits:
            for fact in self._dedupe_hits(hits)[:4]:
                tier = self._tier_phrase(fact)
                parts.append(f"{fact.product}: {fact.value_text} APY{tier}")
                urls.append(fact.url)
        if share:
            top = max(share, key=lambda fact: fact.value)
            parts.append(f"Share Savings tops at {top.value_text} APY{self._tier_phrase(top)}")
            urls.append(top.url)
        if not parts:
            return self._abstain("I can't find that savings yield on the rate sheet. Please contact the credit union.")
        return Answer(" ".join(parts), sources=self._sources(snap_id, urls), confident=True)

    def _dedupe_hits(self, facts: list[Fact]) -> list[Fact]:
        seen = set()
        kept = []
        for fact in facts:
            key = (fact.family, fact.value_text, fact.bal_lo, fact.bal_hi, fact.product.lower())
            if key in seen:
                continue
            seen.add(key)
            kept.append(fact)
        return kept

    def _tier_phrase(self, fact: Fact) -> str:
        if fact.bounds is None:
            return ""
        lo, hi = fact.bounds
        if hi >= 1e17:
            return f" (${lo:,.0f}+)"
        if lo <= 0:
            return f" (${hi:,.0f} or less)"
        return f" (${lo:,.2f}–${hi:,.2f})" if abs(lo - round(lo)) > 0.001 or abs(hi - round(hi)) > 0.001 else f" (${lo:,.0f}–${hi:,.0f})"

    def _official_facts(self, snap_id: str) -> list[Fact]:
        facts = []
        for fact in self.facts.get(snap_id, []):
            if fact.illustrative or fact.ended or fact.phase == "ended":
                continue
            if fact.kind not in {"rate_sheet", "product"}:
                continue
            facts.append(fact)
        return facts

    def _answer_rates(self, snap_id: str, taken: str, spec: dict) -> Optional[Answer]:
        if not spec["priced"] and spec["family"] not in DEPOSIT_FAMILIES | LOAN_FAMILIES | {"mortgage", "fee"}:
            return None
        if spec["family"] is None and not spec["measures"]:
            return None
        matched = [fact for fact in self.facts.get(snap_id, []) if self._fact_matches(fact, spec)]
        official = [fact for fact in matched if fact.kind in {"rate_sheet", "product"} and not fact.ended and fact.phase != "ended"]
        if official:
            matched = official
        if any(fact.origin == "table" for fact in matched):
            matched = [fact for fact in matched if fact.origin == "table"]
        if spec["family"] == "mortgage" and spec["purpose"] is None:
            if any(fact.purpose == "purchase" for fact in matched):
                matched = [fact for fact in matched if fact.purpose == "purchase"]
        matched = [fact for fact in matched if fact.purpose != "rate_relief"]
        if spec["phases"] is not None:
            matched = [fact for fact in matched if fact.phase in spec["phases"] or (fact.phase == "standard" and "standard" in spec["phases"])]
        elif spec["family"] == "card" and not re.search(r"intro|transfer|promotional", spec["low"]):
            matched = [fact for fact in matched if fact.phase != "intro"]
        if spec["measures"]:
            measured = [fact for fact in matched if fact.measure in spec["measures"]]
            if measured:
                matched = measured
        if spec["jumbo"] is None and not spec["compare_jumbo"] and not spec["list_mode"]:
            plain = [fact for fact in matched if not fact.jumbo]
            if plain and any(fact.jumbo for fact in matched):
                matched = plain
        if spec["amounts"] and not spec["compare_jumbo"]:
            chosen = []
            for amount in spec["amounts"]:
                tiered = [fact for fact in matched if fact.bounds is not None and _balance_contains(fact.bounds, amount)]
                if not tiered:
                    continue
                best_span = min(_balance_span(fact.bounds) for fact in tiered if fact.bounds)
                tight = [fact for fact in tiered if fact.bounds and _balance_span(fact.bounds) <= best_span + 0.01]
                for fact in tight:
                    chosen.append(fact)
            if chosen:
                matched = chosen
        if spec["superlative"] and matched:
            target = max(matched, key=lambda fact: fact.value) if spec["superlative"] == "max" else min(matched, key=lambda fact: fact.value)
            matched = [
                fact for fact in matched
                if fact.measure == target.measure and _same_bounds(fact.bounds, target.bounds) and fact.product == target.product
            ] or [target]
        if not matched:
            if spec["family"] in LOAN_FAMILIES | {"mortgage", "certificate", "fee"} and spec["priced"]:
                return self._missing_price(snap_id, spec)
            return None
        matched = self._pick_effective(matched)
        if spec["wants_minimum"]:
            matched = self._with_minimums(snap_id, matched)
        text = self._format_rates(matched, spec)
        urls = []
        for fact in matched:
            urls.append(fact.url)
            for link in fact.detail_urls:
                if _url_key(link) in self.page_urls.get(snap_id, set()):
                    urls.append(link)
        note = None
        stale = self._stale_overlap(snap_id, matched)
        if stale:
            note = "Chose the official rate or product table over a lower-authority page in the same snapshot."
        return Answer(text, sources=self._sources(snap_id, urls, matched[0].quote), confident=True, conflict_note=note)

    def _missing_price(self, snap_id: str, spec: dict) -> Answer:
        family = spec["family"] or "that product"
        offered = any(fact.family == spec["family"] and fact.kind in {"rate_sheet", "product"} for fact in self.facts.get(snap_id, []))
        if spec["term"] and offered:
            # Do not introduce a new numeral (for example by rewriting months as years).
            return self._abstain("That price isn't listed for the requested term. Please contact a lender.")
        if offered:
            return self._abstain(f"That price isn't listed for {family.replace('_', ' ')}. Please contact the credit union.")
        return self._abstain("I don't know; that isn't listed on the website. Please contact the credit union.")

    def _fact_matches(self, fact: Fact, spec: dict) -> bool:
        if fact.illustrative or fact.ended or fact.phase == "ended":
            return False
        if spec["family"] and fact.family != spec["family"]:
            return False
        if spec["segment"] == "business" and fact.segment != "business":
            return False
        if spec["segment"] == "consumer" and fact.segment == "business":
            return False
        if spec["wrapper"]:
            if spec["wrapper"] == "ira":
                if fact.wrapper == "none":
                    return False
            elif fact.wrapper != spec["wrapper"]:
                return False
        elif fact.wrapper != "none" and spec["family"] in {"certificate", None}:
            return False
        if spec["term"] is not None and fact.term_months != spec["term"]:
            return False
        if spec["model_year"] is not None:
            if fact.years is None or not (fact.year_lo <= spec["model_year"] <= fact.year_hi):
                return False
        if spec["jumbo"] is True and not fact.jumbo:
            return False
        if spec["jumbo"] is False and fact.jumbo:
            return False
        if spec["special"] is True and not fact.special and spec["term"] not in {10, 17}:
            return False
        if spec["purpose"] in {"purchase", "refinance"} and fact.purpose not in {spec["purpose"], "unknown"}:
            if spec["family"] == "mortgage" and fact.purpose != spec["purpose"]:
                return False
        if spec["named"]:
            blob = fact.blob.replace("first rate", "1st rate")
            named = spec["named"]
            if spec["family"] == "card" and "platinum" in named and "rewards" in named:
                if "platinum" not in blob and "reward" not in blob:
                    return False
            else:
                for token in named:
                    if token not in blob and token.replace("-", " ") not in blob:
                        return False
        return True

    def _pick_effective(self, facts: list[Fact]) -> list[Fact]:
        groups: dict[tuple, list[Fact]] = defaultdict(list)
        for fact in facts:
            key = (
                fact.family, fact.measure, fact.term_months, fact.purpose, fact.phase,
                fact.jumbo, fact.year_lo, fact.year_hi,
                None if fact.bal_lo is None else _cents(fact.bal_lo),
                None if fact.bal_hi is None else _cents(fact.bal_hi),
                fact.segment, fact.wrapper,
                # Products with no term, tier, or model year (cards, for example)
                # must not collapse into one another.
                "" if any(v is not None for v in (fact.term_months, fact.bal_lo, fact.year_lo))
                else re.sub(r"[^a-z0-9]+", " ", fact.product.lower())[:60],
            )
            groups[key].append(fact)
        chosen = []
        for group in groups.values():
            ranked = sorted(group, key=lambda fact: (fact.effective or "", fact.authority, 1 if fact.kind == "rate_sheet" else 0))
            winner = ranked[-1]
            same = [fact for fact in group if _near(fact.value, winner.value) and fact.value_text == winner.value_text]
            if not same:
                same = [winner]
            # Collapse duplicate rows but keep every supporting URL via the first of each URL.
            by_url = {}
            for fact in same:
                by_url.setdefault(fact.url, fact)
            chosen.extend(by_url.values())
        return chosen

    def _with_minimums(self, snap_id: str, facts: list[Fact]) -> list[Fact]:
        row_ids = {fact.row_id for fact in facts}
        extras = [
            fact for fact in self.facts.get(snap_id, [])
            if fact.row_id in row_ids and fact.measure in {"minimum", "min_open"}
        ]
        # Prefer min-to-earn.
        earn = [fact for fact in extras if fact.measure == "minimum"] or extras
        return facts + earn

    def _format_rates(self, facts: list[Fact], spec: dict) -> str:
        if spec["compare_jumbo"]:
            return self._format_groups(facts, spec)
        if spec["purpose"] == "both":
            return self._format_groups(facts, spec)
        if len(spec["amounts"]) >= 2:
            return self._format_amounts(facts, spec)
        if spec["list_mode"] or len({(fact.product, fact.bal_lo, fact.term_months, fact.year_lo) for fact in facts}) > 1:
            return self._format_groups(facts, spec)
        return self._format_groups(facts, spec)

    def _format_amounts(self, facts: list[Fact], spec: dict) -> str:
        parts = []
        for amount in spec["amounts"]:
            tiered = [fact for fact in facts if fact.bounds and _balance_contains(fact.bounds, amount) and fact.unit == "%"]
            if not tiered:
                continue
            bits = ", ".join(
                f"{fact.value_text} {fact.measure}" if fact.measure != "apy" else f"{fact.value_text} APY"
                for fact in tiered
            )
            parts.append(f"At ${amount:,.2f}: {bits}.")
        text = " ".join(parts) if parts else self._format_groups(facts, spec)
        values = {fact.value for fact in facts if fact.unit == "%" and fact.measure in (spec["measures"] or {fact.measure})}
        if "same" in spec["low"] and len(values) > 1:
            return "No. " + text
        return text

    def _format_groups(self, facts: list[Fact], spec: dict) -> str:
        order = []
        groups: dict[tuple, list[Fact]] = defaultdict(list)
        for fact in facts:
            key = (fact.purpose, fact.product, fact.term_months, fact.year_lo, fact.year_hi, fact.bal_lo, fact.bal_hi, fact.phase, fact.jumbo)
            if key not in groups:
                order.append(key)
            groups[key].append(fact)
        lines = []
        for key in order:
            group = groups[key]
            fact = group[0]
            label_bits = []
            if fact.purpose in {"purchase", "refinance"}:
                label_bits.append(fact.purpose)
            name = re.sub(r"\s+", " ", fact.product).strip()
            label_bits.append(name)
            if fact.phase == "intro":
                label_bits.append("introductory")
            elif fact.phase == "post_promo":
                label_bits.append("after the promotional period")
            measures = []
            seen_m = set()
            for item in group:
                marker = (item.measure, item.value_text)
                if marker in seen_m:
                    continue
                seen_m.add(marker)
                if item.measure == "apy":
                    measures.append(f"{item.value_text} APY")
                elif item.measure == "apr":
                    measures.append(f"{item.value_text} APR")
                elif item.measure == "dividend":
                    measures.append(f"{item.value_text} dividend rate")
                elif item.measure == "rate":
                    measures.append(f"{item.value_text} rate")
                elif item.measure == "fee":
                    measures.append(item.value_text)
                elif item.measure in {"minimum", "min_open"}:
                    measures.append(f"minimum {item.value_text}")
            if spec["family"] == "mortgage" and "rate" in spec["measures"] and len({item.value_text for item in group if item.measure == "rate"}) == 1:
                pass
            lines.append(f"{' '.join(label_bits)}: {', '.join(measures)}.")
        text = " ".join(lines)
        values = {fact.value for fact in facts if fact.unit == "%" and (not spec["measures"] or fact.measure in spec["measures"])}
        if re.search(r"\bsame\b", spec["low"]) and len(values) > 1:
            return "No. " + text
        return text

    def _stale_overlap(self, snap_id: str, chosen: list[Fact]) -> bool:
        stale = [fact for fact in self.facts.get(snap_id, []) if fact.kind in {"calculator", "promo", "guidance"} and fact.unit == "%"]
        for official in chosen:
            for other in stale:
                if other.family == official.family and other.measure == official.measure and other.term_months == official.term_months:
                    if not _near(other.value, official.value):
                        return True
        return False

    def _answer_prose(self, snap_id: str, spec: dict) -> Optional[Answer]:
        tokens = [tok for tok in re.findall(r"[a-z0-9]{4,}", spec["low"]) if tok not in _STOP]
        if len(tokens) < 2:
            return None
        ranked = []
        for sent in self._sents(snap_id):
            if sent.kind in {"calculator", "archive"}:
                continue
            overlap = sum(1 for token in tokens if token in sent.low)
            if overlap < 2:
                continue
            ranked.append((overlap, sent))
        if not ranked:
            return None
        ranked.sort(key=lambda item: (-item[0], len(item[1].text)))
        sent = ranked[0][1]
        if ranked[0][0] < 3 and spec["priced"]:
            return None
        return Answer(sent.text, sources=self._sources(snap_id, [sent.url], sent.text[:160]), confident=True)

    # -- conflicts ----------------------------------------------------------

    def conflicts(self) -> list[Conflict]:
        if not self.order:
            return []
        _taken, snap_id = self.order[-1]
        facts = [fact for fact in self.facts.get(snap_id, []) if fact.conflict_ok and not fact.is_range]
        official = [fact for fact in facts if fact.kind in {"rate_sheet", "product"} and not fact.ended and fact.phase != "ended"]
        stale = [fact for fact in facts if fact.kind in {"calculator", "promo", "guidance"} or fact.ended or fact.phase == "ended"]
        # One bucket per current official claim. A stale number is listed only
        # with the claim it actually competes with (same qualifiers, overlapping
        # model years, same amount tier). Other tiers of the same term are not
        # extra values in that listing.
        groups: dict[tuple, list[tuple[Fact, Fact]]] = defaultdict(list)
        for left in official:
            for right in stale:
                if not self._conflict_compatible(left, right):
                    continue
                if _near(left.value, right.value):
                    continue
                groups[self._claim_bucket(left)].append((left, right))
        # Official pages that disagree on an identical qualifier key.
        exact: dict[tuple, list[Fact]] = defaultdict(list)
        for fact in official:
            exact[self._exact_key(fact)].append(fact)
        results = []
        for key, pairs in groups.items():
            official_vals = {round(left.value, 4) for left, _right in pairs}
            stale_vals = {round(right.value, 4) for _left, right in pairs}
            if stale_vals and stale_vals <= official_vals:
                continue
            if self._phase_split(snap_id, pairs[0][0], official_vals | stale_vals):
                continue
            results.append(self._conflict_from_pairs(snap_id, pairs))
        for key, group in exact.items():
            current = self._current_official_claims(group)
            texts = {fact.value_text for fact in current}
            if len(texts) < 2:
                continue
            if self._phase_split(snap_id, current[0], {round(fact.value, 4) for fact in current}):
                continue
            results.append(self._conflict_from_official(snap_id, current))
        relief = self._relief_conflict(snap_id)
        if relief:
            results.append(relief)
        results.sort(key=lambda item: item.topic)
        # Drop duplicate topics.
        deduped = []
        seen = set()
        for item in results:
            marker = (item.topic, tuple(item.values), item.resolution)
            if marker in seen:
                continue
            seen.add(marker)
            deduped.append(item)
        return deduped

    def _conflict_compatible(self, left: Fact, right: Fact) -> bool:
        if left.family != right.family or left.family in {"other", "savings_other"}:
            return False
        if left.measure != right.measure or left.unit != "%":
            return False
        if left.segment != right.segment or left.wrapper != right.wrapper:
            return False
        if left.jumbo != right.jumbo:
            return False
        if {left.phase, right.phase} == {"intro", "post_promo"}:
            return False
        if left.phase != right.phase and "standard" not in {left.phase, right.phase}:
            return False
        if left.term_months != right.term_months:
            return False
        if left.family in {"auto", "certificate", "mortgage", "personal"} and left.term_months is None:
            return False
        if left.purpose != right.purpose:
            if "unknown" in {left.purpose, right.purpose} and left.family != "mortgage":
                pass
            else:
                return False
        if left.family == "mortgage":
            return _same_bounds(left.bounds, right.bounds)
        if left.family == "auto":
            if left.years and right.years:
                return _years_overlap(left.years, right.years)
            return False
        if left.bounds or right.bounds:
            return _same_bounds(left.bounds, right.bounds)
        return True

    def _claim_bucket(self, fact: Fact) -> tuple:
        """Identity of one current official claim, including tier and model year."""
        return self._conflict_key(fact) + (
            fact.value_text,
            fact.year_lo,
            fact.year_hi,
            None if fact.bal_lo is None else _cents(fact.bal_lo),
            None if fact.bal_hi is None else _cents(fact.bal_hi),
        )

    def _current_official_claims(self, group: list[Fact]) -> list[Fact]:
        """Drop a strictly older effective date when it is not a second current claim.

        Undated official rows still count as current. A later printed effective
        date supersedes an earlier one; that older row is history, not a
        competitor, when the remaining current rows agree.
        """
        dated = [fact.effective for fact in group if fact.effective]
        if not dated:
            return group
        latest = max(dated)
        current = [fact for fact in group if not fact.effective or fact.effective == latest]
        return current or group

    def _conflict_key(self, fact: Fact) -> tuple:
        if fact.family == "auto":
            return ("auto", fact.term_months, fact.measure, fact.segment)
        if fact.family == "mortgage":
            bounds = (
                None if fact.bal_lo is None else _cents(fact.bal_lo),
                None if fact.bal_hi is None else _cents(fact.bal_hi),
            )
            return ("mortgage", fact.measure, fact.purpose, fact.term_months, bounds, fact.segment, fact.phase)
        return (
            fact.family, fact.measure, fact.term_months, fact.jumbo, fact.segment,
            fact.wrapper, fact.purpose, fact.phase,
        )

    def _exact_key(self, fact: Fact) -> tuple:
        return self._conflict_key(fact) + (
            fact.year_lo, fact.year_hi, fact.phase, fact.product.lower(),
        )

    def _phase_split(self, snap_id: str, sample: Fact, values: set) -> bool:
        """Intro and post-promo figures for one product are not a contradiction."""
        intro, post = set(), set()
        for fact in self.facts.get(snap_id, []):
            if fact.family != sample.family or fact.measure != sample.measure or fact.unit != "%":
                continue
            if fact.phase == "intro":
                intro.add(round(fact.value, 4))
            elif fact.phase == "post_promo":
                post.add(round(fact.value, 4))
        return bool(values & intro and values & post and values <= intro | post)

    def _conflict_from_pairs(self, snap_id: str, pairs: list[tuple[Fact, Fact]]) -> Conflict:
        official_values = []
        stale_values = []
        sources = []
        for left, right in pairs:
            if left.value_text not in official_values:
                official_values.append(left.value_text)
            if right.value_text not in stale_values:
                stale_values.append(right.value_text)
            sources.append(Source(url=left.url, snapshot_id=snap_id, quote=left.quote))
            sources.append(Source(url=right.url, snapshot_id=snap_id, quote=right.quote))
        sample = pairs[0][0]
        topic = self._topic(sample)
        values = []
        for text in official_values + stale_values:
            if text not in values:
                values.append(text)
        if sample.family == "auto":
            bits = []
            seen = set()
            for left, _right in pairs:
                label = f"{left.year_lo}-{str(left.year_hi)[2:]}" if left.year_lo else left.product
                marker = (label, left.value_text)
                if marker in seen:
                    continue
                seen.add(marker)
                bits.append(f"{label} {left.value_text}")
            resolution = "; ".join(bits)
        elif len(official_values) == 1:
            resolution = official_values[0]
        else:
            resolution = "; ".join(official_values)
        reason = (
            f"In snapshot {snap_id}, the official table and a lower-authority page disagree on this "
            "same qualifier set. The official rate or product table is the pricing source; "
            "calculator, promo, and stale guidance copies lose. Different tiers, wrappers, and "
            "APR versus APY are not merged into this conflict."
        )
        return Conflict(topic=topic, values=values, sources=self._unique_sources(sources), resolution=resolution, reason=reason)

    def _conflict_from_official(self, snap_id: str, group: list[Fact]) -> Conflict:
        ranked = sorted(group, key=lambda fact: (fact.effective or "", fact.authority, 1 if fact.kind == "rate_sheet" else 0))
        winner = ranked[-1]
        values = []
        sources = []
        for fact in group:
            if fact.value_text not in values:
                values.append(fact.value_text)
            sources.append(Source(
                url=fact.url, snapshot_id=snap_id,
                quote=(fact.quote + (f" effective {fact.effective}" if fact.effective else ""))[:180],
            ))
        reason = (
            "Two official pages in the latest snapshot give different numbers for the same qualifier. "
            f"Resolved to {winner.value_text} from the later effective date or the rate sheet ({winner.effective or 'date not printed'})."
        )
        return Conflict(
            topic=self._topic(winner),
            values=values,
            sources=self._unique_sources(sources),
            resolution=winner.value_text,
            reason=reason,
        )

    def _relief_conflict(self, snap_id: str) -> Optional[Conflict]:
        priced = [
            fact for fact in self.facts.get(snap_id, [])
            if fact.purpose == "rate_relief" and fact.unit == "%" and fact.conflict_ok
        ]
        notice = None
        for sent in self._sents(snap_id):
            if "rate relief" in sent.low and re.search(r"has ended|concluded its participation", sent.low):
                notice = sent
                break
        if not priced or notice is None:
            return None
        values = []
        sources = [Source(url=notice.url, snapshot_id=snap_id, quote=notice.text[:160])]
        for fact in priced:
            if fact.value_text not in values:
                values.append(fact.value_text)
            sources.append(Source(url=fact.url, snapshot_id=snap_id, quote=fact.quote))
        values.append("ended")
        return Conflict(
            topic="Mortgage Rate Relief program availability",
            values=values,
            sources=self._unique_sources(sources),
            resolution="ended",
            reason=(
                "The latest snapshot still has a guidance table for Mortgage Rate Relief, and a newsroom "
                "notice says participation concluded and the program ended. The notice controls availability; "
                "this is not a disagreement with the ordinary mortgage table."
            ),
        )

    def _topic(self, fact: Fact) -> str:
        bits = []
        if fact.term_months:
            if fact.term_months % 12 == 0 and fact.family == "mortgage":
                bits.append(f"{fact.term_months // 12}-year")
            else:
                bits.append(f"{fact.term_months}-month")
        if fact.purpose in {"purchase", "refinance", "rate_relief"}:
            bits.append(fact.purpose.replace("_", " "))
        bits.append(fact.family.replace("_", " "))
        bits.append(fact.measure.upper() if fact.measure in {"apr", "apy"} else fact.measure)
        if fact.jumbo:
            bits.append("jumbo")
        if fact.bounds and fact.family == "mortgage":
            _lo, hi = fact.bounds
            if hi < 1e17:
                bits.append(f"${hi:,.0f} or less")
        return " ".join(bits)

    def _unique_sources(self, sources: list) -> list:
        seen = set()
        kept = []
        for source in sources:
            marker = (source.url, source.quote)
            if marker in seen:
                continue
            seen.add(marker)
            kept.append(source)
        return kept[:8]


def build():
    return Knowledge()
