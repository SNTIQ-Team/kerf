"""Shared helpers for HTML portals whose decisions carry no Randnummern (BB, NI).

``leaf_blocks`` flattens arbitrarily nested markup (``<p><p>..</p></p>``,
``<p><body><div><blockquote>..``) into the innermost text-bearing blocks in
document order.  It never invents labels: portals without explicit
Randnummern yield unlabelled blocks, which ``paragraphs.assemble`` turns into
stable ``#p-<n>`` ids.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta

from bs4 import Comment, NavigableString, Tag

from ..errors import UpstreamFailure
from ..detect import classify_page
from ..model import Block

BLOCK_TAGS = frozenset({
    "p", "div", "blockquote", "body", "html", "li", "ol", "ul", "table", "h1", "h2", "h3", "h4", "h5",
    "h6", "dl", "dd", "dt", "section", "article", "center", "pre", "header", "footer", "main", "form",
    "fieldset", "address", "figure", "hr", "aside", "nav"})
SKIP_TAGS = frozenset({"script", "style", "noscript", "template"})
OUTLINE = re.compile(r"(?:[IVXLC]{1,6}|[A-Za-z]{1,2}|\d{1,2})[.)]|\([a-z]{1,2}\)|[a-z]{1,2}\)\)")


def clean_line(text: str) -> str:
    return " ".join(text.replace("­", "").split())


def inline_text(nodes) -> str:
    """Visible text of inline content; ``<br>`` becomes a line break."""
    def walk(el) -> str:
        if isinstance(el, Comment):
            return ""
        if isinstance(el, NavigableString):
            return str(el)
        if not isinstance(el, Tag) or el.name in SKIP_TAGS:
            return ""
        if el.name == "br":
            return "\n"
        return "".join(walk(c) for c in el.children)
    raw = "".join(walk(n) for n in nodes)
    return "\n".join(line for line in (clean_line(part) for part in raw.split("\n")) if line)


def table_text(table: Tag) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [inline_text([c]).replace("\n", " ") for c in tr.find_all(["td", "th"], recursive=False)]
        if any(cells):
            rows.append("\t".join(cells))
    return "\n".join(rows)


def is_outline_marker(text: str) -> bool:
    """'I.', 'II.', '1.', 'a)', '(aa)': structural heading lines without own text."""
    return bool(OUTLINE.fullmatch(text.strip()))


def leaf_blocks(node: Tag, locator: str) -> list[Block]:
    """Innermost text blocks of ``node`` in document order (no labels)."""
    out: list[Block] = []

    def emit(text: str, loc: str) -> None:
        if text.strip():
            out.append(Block(text=text, kind="heading" if is_outline_marker(text) else "text", locator=loc))

    def visit(el: Tag, loc: str) -> None:
        if el.name in SKIP_TAGS:
            return
        if el.name == "table":
            emit(table_text(el), loc)
            return
        children = list(el.children)
        if not any(isinstance(c, Tag) and c.name in BLOCK_TAGS for c in children):
            emit(inline_text(children), loc)
            return
        run: list = []
        counts: dict[str, int] = {}
        runs = 0

        def flush() -> None:
            nonlocal runs
            if run and inline_text(run).strip():
                runs += 1
                emit(inline_text(run), f"{loc}/text()[{runs}]")
            run.clear()
        for child in children:
            if isinstance(child, Tag) and child.name in BLOCK_TAGS:
                flush()
                counts[child.name] = counts.get(child.name, 0) + 1
                child_loc = f"#{child['id']}" if child.get("id") else f"{loc}/{child.name}[{counts[child.name]}]"
                visit(child, child_loc)
            else:
                run.append(child)
        flush()

    visit(node, locator)
    return out


def ensure_document(markup: str, ok: bool, *, url: str, what: str) -> None:
    """Raise a typed failure when a fetched page is not the expected document."""
    if ok:
        return
    verdict = classify_page(markup.encode("utf-8", "replace"))
    if verdict:
        kind, evidence = verdict
        raise UpstreamFailure(kind, evidence, url=url)
    raise UpstreamFailure("unexpected_representation", f"not a {what}", url=url)


ECLI_RE = re.compile(r"ECLI:DE:[A-Z0-9]{2,}:\d{4}:\d{4}[A-Z]{0,2}\.[0-9A-Z.]*[0-9A-Z]\.\d{2}")


def checked_ecli(raw: str | None, decision_date: str, metadata: dict) -> str | None:
    """Return a structurally valid ECLI; malformed ones (e.g. without Az part,
    'ECLI:DE:SGCOTTB:2023:0525.00') are kept only in metadata because they are
    not unique and must not become a decision identity."""
    value = clean_line(raw or "")
    if not value or value in {"-", "[keine Angabe]"}:
        return None
    if not ECLI_RE.fullmatch(value):
        metadata["ecli_published_invalid"] = value
        return None
    parts = value.split(":")
    if parts[3] != decision_date[:4] or parts[4][:4] != decision_date[5:7] + decision_date[8:10]:
        metadata["ecli_date_mismatch"] = True
    return value


def item_extra(item) -> dict:
    extra = item["extra"] if "extra" in (item.keys() if hasattr(item, "keys") else item) else None
    if isinstance(extra, str):
        try:
            return json.loads(extra or "{}")
        except ValueError:
            return {}
    return dict(extra or {})


def incremental_since(conn: sqlite3.Connection | None, source_id: str, overlap_days: int) -> str | None:
    """Window start for an unbounded re-sync: last complete unbounded run minus an overlap
    (publication lags decision dates).  None on first run -> full backfill."""
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT max(started_at) FROM sync_runs WHERE source_id=? AND status IN ('ok','partial') "
                           "AND limit_n IS NULL AND since IS NULL", (source_id,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    started = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00")).date()
    return (started - timedelta(days=overlap_days)).isoformat()


def german_date(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d.%m.%Y")
