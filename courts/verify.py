"""Cross-representation verification (operating principle 4).

The same decision is parsed from an independent representation and compared
with the stored one:

    nrw        NRWE HTML (stored)        vs  NRWE PDF (right-margin Randnummern)
    rii        RII XML (stored, BVerwG)  vs  bverwg.de HTML
    bverwg     bverwg.de HTML (stored)   vs  RII XML (from the RII listing)
    any        stored capture            vs  a second official source's capture of the same ECLI
                                              (items skipped as duplicate_identity; offline, from the packs)

Compared: numbering, Randnummer count, Randnummer boundaries, per-Rn text
hashes after normalisation, case numbers.  Disagreements become typed rows in
``verification_findings``; the stored decision is never changed and neither
representation is picked automatically.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import random
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Callable

from .casenumbers import case_key, split_case_numbers
from .errors import UpstreamFailure
from .model import Block, ParsedDecision, Section
from .paragraphs import assemble
from .sources.base import Capture, iso_date, squash

VERIFIER_VERSION = "courts-verify/1.0.0"
SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_runs(
  id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, sample INTEGER, pairs INTEGER DEFAULT 0,
  agreeing INTEGER DEFAULT 0, findings INTEGER DEFAULT 0, errors INTEGER DEFAULT 0, verifier_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_checks(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, decision_id TEXT NOT NULL, representation_a TEXT NOT NULL,
  representation_b TEXT NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN ('agree','disagree','error')),
  detail TEXT NOT NULL DEFAULT '{}', checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS verification_checks_decision ON verification_checks(decision_id);
CREATE TABLE IF NOT EXISTS verification_findings(
  id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, decision_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('numbering_mismatch','rn_count_mismatch','rn_boundary_mismatch',
                                    'text_mismatch','case_numbers_mismatch','representation_error')),
  severity TEXT NOT NULL CHECK(severity IN ('major','minor')),
  representation_a TEXT NOT NULL, representation_b TEXT NOT NULL, locator TEXT,
  value_a TEXT, value_b TEXT, detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','explained','corrected'))
);
CREATE INDEX IF NOT EXISTS verification_findings_decision ON verification_findings(decision_id);
CREATE TABLE IF NOT EXISTS verification_captures(
  decision_id TEXT NOT NULL, representation TEXT NOT NULL, sha256 TEXT NOT NULL, url TEXT NOT NULL,
  fetched_at TEXT NOT NULL, PRIMARY KEY(decision_id, representation)
);
"""


# Numbering-free body comparison: the reasoning text; headnotes are positional and printed differently
# (NRWE PDFs put them into the head field table), the remedy instructions are split off by the model.
BODY_EXCLUDED = {"titel", "orientierungssatz", "leitsatz", "leitsaetze", "rechtsmittelbelehrung"}


def normalise(text: str) -> str:
    """Representation-neutral text: NFKC, no soft hyphens, no line-break hyphenation, letters/digits only."""
    text = unicodedata.normalize("NFKC", text).replace("­", "")
    text = re.sub(r"(?<=[a-zäöüß])-\s*\n\s*(?=[a-zäöüß])", "", text)
    return re.sub(r"[\W_]+", "", text.casefold())


def text_hash(text: str) -> str:
    return hashlib.sha256(normalise(text).encode()).hexdigest()


@dataclass
class Representation:
    label: str
    numbering: str
    rn_texts: dict[str, str]          # Rn label -> text (explicitly numbered paragraphs only)
    body_text: str                    # all court paragraphs, for numbering-free comparison
    case_numbers: list[str]


def from_paragraphs(label: str, paragraphs: list[tuple[str | None, str, str]], numbering: str,
                    case_numbers: list[str]) -> Representation:
    """paragraphs: (rn, section, text) in document order."""
    rn = {}
    for label_rn, _section, text in paragraphs:
        if label_rn and label_rn not in rn:
            rn[label_rn] = text
    body = "\n".join(t for _rn, section, t in paragraphs if section not in BODY_EXCLUDED)
    return Representation(label, numbering, rn, body, case_numbers)


def stored_representation(conn: sqlite3.Connection, decision_id: str) -> Representation:
    d = conn.execute("SELECT source_id, numbering, case_numbers FROM decisions WHERE id=?", (decision_id,)).fetchone()
    rows = conn.execute("SELECT rn, section, text FROM paragraphs WHERE decision_id=? ORDER BY ordinal",
                        (decision_id,)).fetchall()
    return from_paragraphs(f"stored:{d['source_id']}", [tuple(r) for r in rows], d["numbering"],
                           json.loads(d["case_numbers"]))


def parsed_representation(label: str, parsed: ParsedDecision) -> Representation:
    from .store import split_remedy_instructions  # the same model rule as for stored decisions
    paragraphs, numbering = assemble("X", split_remedy_instructions(parsed.sections))
    return from_paragraphs(label, [(p.rn, p.section, p.text) for p in paragraphs], numbering, parsed.case_numbers)


URL = re.compile(r"(?:https?://|www\.)\S+(?:(?<=[-/_.=&?])\s+[^\s(]+)*")  # a URL wrapped at - / _ continues
LIST_MARKER = re.compile(r"^\s*(?:\d{1,2}|[a-z]{1,2}|[ivx]{1,4})[.)]\s+")


def explain_difference(text_a: str, text_b: str) -> str | None:
    """A known representation difference that leaves the Rn assignment intact:
    url_clipped    PDFs clip overlong URLs at the page edge;
    list_marker    an <ol> number printed in one representation only ("4. (entfällt)");
    reading_order  the same words in another order (table cells, multi-column layout)."""
    a, b = URL.sub(" ", text_a), URL.sub(" ", text_b)
    if normalise(a) == normalise(b):
        return "url_clipped"
    if normalise(LIST_MARKER.sub("", a)) == normalise(LIST_MARKER.sub("", b)):
        return "list_marker"
    from collections import Counter
    words = lambda t: Counter(re.findall(r"[^\W_]+", unicodedata.normalize("NFKC", t).replace("\u00ad", "").casefold()))
    if words(LIST_MARKER.sub("", a)) == words(LIST_MARKER.sub("", b)):
        return "reading_order"
    return None


def compare(a: Representation, b: Representation) -> list[dict]:
    findings: list[dict] = []

    def add(kind, severity, locator=None, value_a=None, value_b=None, **detail):
        findings.append({"kind": kind, "severity": severity, "locator": locator,
                         "value_a": None if value_a is None else json.dumps(value_a, ensure_ascii=False),
                         "value_b": None if value_b is None else json.dumps(value_b, ensure_ascii=False),
                         "detail": detail})
    keys_a, keys_b = {case_key(c) for c in a.case_numbers}, {case_key(c) for c in b.case_numbers}
    if keys_a != keys_b:
        if keys_a and keys_b and (keys_a < keys_b or keys_b < keys_a):
            # One representation lists related dockets the other omits (RII adds the Az under which a
            # Nichtzulassungsbeschwerde continues as Revision, "5 B 4.26" + "5 C 5.26"); the decision's own
            # docket agrees.
            add("case_numbers_mismatch", "minor", None, a.case_numbers, b.case_numbers, explained="docket_subset")
        else:
            add("case_numbers_mismatch", "major", None, a.case_numbers, b.case_numbers)
    if a.numbering != b.numbering:
        if a.numbering == "rn" and b.numbering == "none":
            # The stored copy has the explicit Rn, the alternate prints none (bsg.bund.de, some Land portals):
            # the precedence rule chose correctly; the texts are still compared numbering-free below.
            add("numbering_mismatch", "minor", None, a.numbering, b.numbering, explained="alternate_unnumbered")
        else:
            add("numbering_mismatch", "major", None, a.numbering, b.numbering)
    if a.numbering == "rn" and b.numbering == "rn":
        if len(a.rn_texts) != len(b.rn_texts):
            add("rn_count_mismatch", "major", None, len(a.rn_texts), len(b.rn_texts),
                only_a=sorted(set(a.rn_texts) - set(b.rn_texts), key=_rn_key)[:20],
                only_b=sorted(set(b.rn_texts) - set(a.rn_texts), key=_rn_key)[:20])
        labels = sorted(set(a.rn_texts) & set(b.rn_texts), key=_rn_key)
        order_a, order_b = sorted(a.rn_texts, key=_rn_key), sorted(b.rn_texts, key=_rn_key)

        def following(order, rn):
            i = order.index(rn)
            return order[i + 1] if i + 1 < len(order) else None
        for index, rn in enumerate(labels):
            ta, tb = normalise(a.rn_texts[rn]), normalise(b.rn_texts[rn])
            if ta == tb:
                continue
            ratio = difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio() if len(ta) + len(tb) < 40000 else \
                difflib.SequenceMatcher(None, ta, tb).quick_ratio()
            # A boundary shift: one representation's Rn n swallowed (the end of) the other's Rn n+1.
            next_a, next_b = following(order_a, rn), following(order_b, rn)
            nxt = next_a or next_b
            shifted = (next_a is not None and tb.endswith(normalise(a.rn_texts[next_a])[-80:])) or \
                (next_b is not None and ta.endswith(normalise(b.rn_texts[next_b])[-80:]))
            if shifted:
                add("rn_boundary_mismatch", "major", f"rn-{rn}", a.rn_texts[rn][:300], b.rn_texts[rn][:300],
                    next_rn=nxt, similarity=round(ratio, 4))
            else:
                explained = explain_difference(a.rn_texts[rn], b.rn_texts[rn])
                add("text_mismatch", "minor" if ratio >= 0.97 or explained else "major", f"rn-{rn}",
                    a.rn_texts[rn][:300], b.rn_texts[rn][:300], similarity=round(ratio, 4),
                    hash_a=text_hash(a.rn_texts[rn]), hash_b=text_hash(b.rn_texts[rn]),
                    **({"explained": explained} if explained else {}))
    else:
        ta, tb = normalise(a.body_text), normalise(b.body_text)
        if ta != tb:
            ratio = difflib.SequenceMatcher(None, ta, tb).quick_ratio()
            explained = explain_difference(a.body_text, b.body_text)
            add("text_mismatch", "minor" if ratio >= 0.97 or explained else "major", "body", None, None,
                similarity=round(ratio, 4), hash_a=text_hash(a.body_text), hash_b=text_hash(b.body_text),
                **({"explained": explained} if explained else {}))
    return findings


def _rn_key(label: str):
    m = re.match(r"(\d+)(.*)", label)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, label)


# --- NRWE PDF: an independent parser for the second representation --------------------------------
#
# NRWE renders the HTML decision to PDF with the Randnummern right-aligned in a margin column.  The reader
# works on word boxes (pdftotext -bbox-layout), not on the space-padded -layout text:
#   * the Rn column is the dominant right edge (xMax) of numeric words in the outer fifth of the page; a
#     label is a numeric word whose right edge lies on that column (a justified line can end one space, or
#     no space, before the label in -layout text, and a docket such as "S 18" can end a text line);
#   * text lines belong to the latest label at or above them (page, y); page breaks never start an Rn;
#     nested list items are indented by ~4pt per level, so the column is a narrow band right of the mode;
#   * stacked labels - several labels on one row - are nested list wrappers (NRWE numbers the <ol> and its
#     first <li>): all but the last have no own text and the row text belongs to the last label, exactly as
#     the HTML model records it (metadata.rn_without_own_text).

HEAD_FIELD = re.compile(r"^(Datum|Gericht|Spruchkörper|Entscheidungsart|Aktenzeichen|ECLI):\s+(.+)$")
RN_WORD = re.compile(r"^\d{1,3}[a-z]?$")


def _sequence_ok(previous: str | None, label: str) -> bool:
    number, suffix = _rn_key(label)
    if previous is None:
        return number == 1 and not suffix
    last, last_suffix = _rn_key(previous)
    if number == last:
        return bool(suffix) and suffix > last_suffix          # 12 -> 12a
    return last < number <= last + 3 and not suffix or number == last + 1


def nrwe_pdf_labels(pages: list[dict]) -> tuple[list[dict], list[dict], float]:
    """(labels, text_lines, row_step); labels/lines carry page, y0, y1 (and rn / text).

    A candidate is a numeric word on the Rn column band; it becomes a label only if it continues the
    sequence (first 1, then +1; a letter suffix or a gap of up to two numbers is tolerated), so a stray
    number that happens to sit at the right edge (a table cell, a footnote) stays text."""
    from collections import Counter
    edges = Counter()
    for page in pages:
        for line in page["lines"]:
            for x0, x1, word in line["words"]:
                if RN_WORD.match(word) and x0 > 0.8 * page["width"]:
                    edges[round(x1)] += 1
    column = edges.most_common(1)[0][0] if edges else None
    candidates = []
    for number, page in enumerate(pages):
        page["lines"].sort(key=lambda l: (l["y0"], l["words"][0][0]))
        for li, line in enumerate(page["lines"]):
            for wi, (x0, x1, word) in enumerate(line["words"]):
                if column is not None and RN_WORD.match(word) and x0 > 0.8 * page["width"] \
                        and column - 2.5 <= x1 <= column + 12:
                    candidates.append((number, line["y0"], _rn_key(word), li, wi, word, line["y1"]))
    candidates.sort()
    accepted, previous = set(), None
    labels = []
    for number, y0, _key, li, wi, word, y1 in candidates:
        if _sequence_ok(previous, word):
            accepted.add((number, li, wi))
            labels.append({"page": number, "y0": y0, "y1": y1, "rn": word})
            previous = word
    if len(labels) < 2:
        labels, accepted = [], set()  # a single right-edge number is no Rn column
    texts, steps = [], []
    for number, page in enumerate(pages):
        previous_y = None
        for li, line in enumerate(page["lines"]):
            kept = [word for wi, (_x0, _x1, word) in enumerate(line["words"]) if (number, li, wi) not in accepted]
            if kept:
                texts.append({"page": number, "y0": line["y0"], "y1": line["y1"], "text": " ".join(kept)})
                if previous_y is not None and 5 < line["y0"] - previous_y < 30:
                    steps.append(line["y0"] - previous_y)
                previous_y = line["y0"]
    steps.sort()
    return labels, texts, steps[len(steps) // 2] if steps else 14.0


def nrwe_pdf_blocks(pages: list[dict]) -> tuple[list[Block], list[str]]:
    from .sources.pdftext import join_lines
    labels, texts, step = nrwe_pdf_labels(pages)
    events = []
    for label in labels:
        row = next((t for t in texts if t["page"] == label["page"] and abs(t["y0"] - label["y0"]) < step / 2), None)
        events.append(((label["page"], row["y0"] if row else label["y0"], 0, _rn_key(label["rn"])), "label", label))
    for text in texts:
        events.append(((text["page"], text["y0"], 1, (0, "")), "text", text))
    events.sort(key=lambda e: e[0])
    order: list[str] = []
    collected: dict[str, list[str]] = {}
    wrappers: list[str] = []
    current = None
    since_label: list[dict] = []
    for _key, kind, item in events:
        if kind == "text":
            if current is not None:
                collected[current["rn"]].append(item["text"])
                since_label.append(item)
            continue
        if current is not None and current["page"] == item["page"] and abs(item["y0"] - current["y0"]) < step / 2 \
                and not since_label:
            wrappers.append(current["rn"])
            moved = collected.pop(current["rn"])
            order.remove(current["rn"])
            collected[item["rn"]] = moved
        elif item["rn"] in collected:
            item = dict(item, rn=f"{item['rn']}#dup")  # a repeated label is kept visible for compare()
            collected[item["rn"]] = []
        else:
            collected[item["rn"]] = []
        order.append(item["rn"])
        current, since_label = item, []
    blocks = [Block(text=join_lines(collected[rn]), label=rn, locator="nrwe-pdf") for rn in order]
    return [b for b in blocks if b.text], wrappers


def _paragraph_chunks(lines: list[str]) -> list[list[str]]:
    chunks, current = [], []
    for line in lines:
        if line.strip():
            current.append(line)
        elif current:
            chunks.append(current)
            current = []
    return chunks + ([current] if current else [])


def parse_nrwe_pdf(pdf: bytes, url: str, source_doc_id: str) -> ParsedDecision:
    from .sources.pdftext import join_lines, pdf_text, pdf_word_lines
    text = pdf_text(pdf, url=url)
    lines = [line.rstrip() for line in text.replace("\f", "\n").split("\n")]
    fields: dict[str, str] = {}
    for line in lines[:60]:
        m = HEAD_FIELD.match(line.strip())
        if m and m.group(1) not in fields:
            fields[m.group(1)] = squash(m.group(2))
    if not fields.get("Aktenzeichen") or not fields.get("Datum"):
        raise UpstreamFailure("parse_error", "NRWE PDF head lacks Aktenzeichen/Datum", url=url)
    blocks, wrappers = nrwe_pdf_blocks(pdf_word_lines(pdf, url=url))
    if not blocks:
        # No Rn column: the decision is unnumbered (NRWE prints some Beschlüsse entirely in the Tenor
        # field).  The body - from the Tenor field on - is still compared, numbering-free.
        tenor = next((i for i, line in enumerate(lines) if re.match(r"^Tenor:", line)), None)
        if tenor is None:
            raise UpstreamFailure("parse_error", "NRWE PDF has neither an Rn column nor a Tenor field", url=url)
        body = [re.sub(r"^Tenor:", "", lines[tenor])] + lines[tenor + 1:]
        blocks = [Block(text=join_lines(chunk), locator="nrwe-pdf")
                  for chunk in _paragraph_chunks(body) if join_lines(chunk)]
    raw_az = fields["Aktenzeichen"]
    return ParsedDecision(source_id="nrw-pdf", source_doc_id=source_doc_id, source_url=url,
                          court_name=fields.get("Gericht", ""), decision_date=iso_date(fields["Datum"]),
                          case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
                          decision_type=fields.get("Entscheidungsart"),
                          sections=[Section("gruende", "court", "NRWE PDF body (right-margin Rn column)", blocks)],
                          ecli=fields.get("ECLI"), metadata={"rn_without_own_text": wrappers})


# --- alternates -------------------------------------------------------------------------------------

@dataclass
class Alternate:
    label: str
    load: Callable[[], ParsedDecision]
    network: bool


COURT_SITE_SOURCES = {"bag": "bag", "bsg": "bsg", "bverwg": "bverwg", "bverfg": "bverfg"}


def cached_fetch(conn: sqlite3.Connection, decision_id: str, representation: str, url: str, http, home=None) -> bytes:
    """Fetch an alternate representation once; later verification runs read it from the packs (offline)."""
    from .packs import PackStore
    from .config import data_home
    row = conn.execute("SELECT sha256 FROM verification_captures WHERE decision_id=? AND representation=?",
                       (decision_id, representation)).fetchone()
    store = PackStore(home or data_home(), conn)
    if row is not None:
        return store.get(row[0])
    if http is None:
        raise UpstreamFailure("network", "alternate not cached and verification runs offline", url=url)
    body = http.get(url, timeout=120).body
    ext = "pdf" if body.startswith(b"%PDF") else "zip" if body.startswith(b"PK") else "html"
    # One pack directory per representation: parallel verify processes never append to the same pack.
    ref = store.put(body, f"verify-{representation}", ext)
    conn.execute("INSERT OR REPLACE INTO verification_captures VALUES(?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))",
                 (decision_id, representation, ref.sha256, url))
    return body


def alternates(conn: sqlite3.Connection, decision: sqlite3.Row, http=None, kinds: set[str] | None = None
               ) -> list[Alternate]:
    out: list[Alternate] = []
    from .store import read_capture
    want = kinds or {"capture", "nrwe-pdf", "rii-xml"}
    # 1. offline: a second official source's capture of the same decision (skipped duplicate)
    if "capture" in want:
        for item in conn.execute("""SELECT * FROM source_items WHERE decision_id IS NULL AND state='skipped'
                                    AND failure_kind='duplicate_identity' AND content_hash IS NOT NULL
                                    AND failure_detail LIKE ?""", (decision["id"] + " %",)).fetchall():
            def load(item=item):
                from .sources import get_adapter
                ext = (conn.execute("SELECT extension FROM captures WHERE sha256=?",
                                    (item["content_hash"],)).fetchone() or ["bin"])[0]
                body = read_capture(conn, item["content_hash"])
                return get_adapter(item["source_id"]).parse(Capture(body, item["url"], "", ext), item)
            out.append(Alternate(f"capture:{item['source_id']}", load, False))
    cached = {r[0] for r in conn.execute("SELECT representation FROM verification_captures WHERE decision_id=?",
                                         (decision["id"],))}
    # 2. NRWE: the PDF of the same page
    if "nrwe-pdf" in want and decision["source_id"] == "nrw" and (http is not None or "nrwe-pdf" in cached):
        path = decision["source_url"].split("nrwe.justiz.nrw.de", 1)[-1]
        pdf_url = f"https://nrwe.justiz.nrw.de/pdfdownload/downloadEntscheidung.php?entscheidung=/nrwe{path}"

        def load_pdf():
            body = cached_fetch(conn, decision["id"], "nrwe-pdf", pdf_url, http)
            if not body.startswith(b"%PDF"):
                raise UpstreamFailure("unexpected_representation", "NRWE PDF download is not a PDF", url=pdf_url)
            return parse_nrwe_pdf(body, pdf_url, decision["source_doc_id"])
        out.append(Alternate("nrwe-pdf", load_pdf, "nrwe-pdf" not in cached))
    # 3. court site (BAG, BSG, BVerwG, BVerfG) <-> RII XML of the same decision
    if "rii-xml" in want and decision["source_id"] in COURT_SITE_SOURCES and (http is not None or "rii-xml" in cached):
        keys = {case_key(c) for c in json.loads(decision["case_numbers"])}
        rows = conn.execute("SELECT * FROM source_items WHERE source_id='rii' AND listed_date=? AND listed_court=?",
                            (decision["decision_date"], COURT_SITE_SOURCES[decision["source_id"]])).fetchall()
        match = [r for r in rows if {case_key(c) for c in split_case_numbers(r["listed_case_numbers"] or "")} & keys]
        if len(match) == 1:
            def load_rii(item=match[0]):
                from .sources.rii import RiiAdapter
                body = cached_fetch(conn, decision["id"], "rii-xml", item["url"], http)
                return RiiAdapter().parse(Capture(body, item["url"], "application/zip", "zip"), item)
            out.append(Alternate("rii-xml", load_rii, "rii-xml" not in cached))
    return out


def adopt_verification_captures(conn: sqlite3.Connection) -> int:
    """RII XML fetched for verification is the official RII capture of that item: a skipped RII duplicate
    without its own capture adopts it (same URL, same bytes), so re-examination can apply the explicit-Rn
    precedence offline instead of fetching the item again."""
    adopted = 0
    for d in conn.execute("""SELECT d.*, v.sha256 FROM verification_captures v JOIN decisions d ON d.id = v.decision_id
                             WHERE v.representation = 'rii-xml' AND d.source_id IN (%s)"""
                          % ",".join("?" * len(COURT_SITE_SOURCES)), tuple(COURT_SITE_SOURCES)).fetchall():
        keys = {case_key(c) for c in json.loads(d["case_numbers"])}
        rows = conn.execute("""SELECT * FROM source_items WHERE source_id='rii' AND listed_date=? AND listed_court=?
                               AND state='skipped' AND content_hash IS NULL""",
                            (d["decision_date"], COURT_SITE_SOURCES[d["source_id"]])).fetchall()
        match = [r for r in rows if {case_key(c) for c in split_case_numbers(r["listed_case_numbers"] or "")} & keys]
        if len(match) == 1:
            conn.execute("UPDATE source_items SET content_hash=? WHERE source_id='rii' AND source_doc_id=?",
                         (d["sha256"], match[0]["source_doc_id"]))
            adopted += 1
    conn.commit()
    return adopted


def candidates_by_kind(conn: sqlite3.Connection, *, online: bool, source: str | None) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    rows["capture"] = [r[0] for r in conn.execute(
        """SELECT DISTINCT substr(failure_detail, 1, instr(failure_detail, ' ') - 1) FROM source_items
           WHERE state='skipped' AND content_hash IS NOT NULL AND failure_kind='duplicate_identity'""")]
    cached = conn.execute("SELECT decision_id, representation FROM verification_captures").fetchall()
    for kind in ("nrwe-pdf", "rii-xml"):
        rows[kind] = [d for d, rep in cached if rep == kind]
    if online:
        rows["nrwe-pdf"] += [r[0] for r in conn.execute("SELECT id FROM decisions WHERE source_id='nrw'")]
        rows["rii-xml"] += [r[0] for r in conn.execute(
            f"SELECT id FROM decisions WHERE source_id IN ({','.join('?' * len(COURT_SITE_SOURCES))})",
            tuple(COURT_SITE_SOURCES))]
    if source:
        keep = {r[0] for r in conn.execute("SELECT id FROM decisions WHERE source_id=?", (source,))}
        rows = {k: [d for d in v if d in keep] for k, v in rows.items()}
    return {k: list(dict.fromkeys(v)) for k, v in rows.items()}


def verify_sample(conn: sqlite3.Connection, *, sample: int, http=None, source: str | None = None,
                  seed: int | None = None, kinds: set[str] | None = None, reuse_run: int | None = None) -> dict:
    """Compare up to ``sample`` pairs, stratified over pair kinds; ``reuse_run`` re-checks exactly the pairs of an
    earlier run (offline, from the cached captures)."""
    conn.executescript(SCHEMA)
    run = conn.execute("INSERT INTO verification_runs(started_at, sample, verifier_version) "
                       "VALUES(strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), ?, ?)", (sample, VERIFIER_VERSION)).lastrowid
    rng = random.Random(seed)
    if reuse_run is not None:
        plan = [(r[0], r[1].split(":")[0]) for r in conn.execute(
            "SELECT decision_id, representation_b FROM verification_checks WHERE run_id=? ORDER BY id", (reuse_run,))]
    else:
        pools = candidates_by_kind(conn, online=http is not None, source=source)
        pools = {k: v for k, v in pools.items() if v and (not kinds or k in kinds)}
        for pool in pools.values():
            rng.shuffle(pool)
        if "rii-xml" in pools:  # every court site represented: the smaller ones first, BAG fills the rest
            site = {r[0]: r[1] for r in conn.execute("SELECT id, source_id FROM decisions WHERE source_id IN "
                                                      "('bsg','bverwg','bverfg')")}
            pools["rii-xml"].sort(key=lambda d: 0 if d in site else 1)
        plan, quota = [], {k: max(1, sample // max(1, len(pools))) for k in pools}
        if "capture" in pools:  # offline pairs cost nothing: take them all (within the sample)
            quota["capture"] = min(len(pools["capture"]), sample)
        for kind, pool in pools.items():
            plan += [(d, kind) for d in pool[:quota[kind]]]
    counts = {"pairs": 0, "agreeing": 0, "findings": 0, "errors": 0}
    by_kind: dict[str, dict[str, int]] = {}
    for decision_id, kind in plan:
        decision = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if decision is None:
            continue
        stored = stored_representation(conn, decision_id)
        alts = alternates(conn, decision, http, {kind})
        if not alts and reuse_run is not None:
            # The stored copy changed source since that run (explicit-Rn precedence: RII replaced the
            # unnumbered bsg.bund.de copy), so the same decision is re-checked against what is available.
            alts = alternates(conn, decision, None, None)
        for alt in alts:
            counts["pairs"] += 1
            try:
                other = parsed_representation(alt.label, alt.load())
                findings = compare(stored, other)
                outcome = "disagree" if findings else "agree"
            except (UpstreamFailure, ValueError, OSError) as exc:
                findings = [{"kind": "representation_error", "severity": "minor", "locator": None, "value_a": None,
                             "value_b": None, "detail": {"error": f"{type(exc).__name__}: {exc}"[:400]}}]
                outcome = "error"
            stat = by_kind.setdefault(alt.label.split(":")[0], {"pairs": 0, "agree": 0, "disagree": 0, "error": 0,
                                                                "major_findings": 0})
            stat["pairs"] += 1
            stat[outcome] += 1
            stat["major_findings"] += sum(1 for f in findings if f["severity"] == "major")
            counts["agreeing" if outcome == "agree" else ("errors" if outcome == "error" else "findings")] += 1
            conn.execute("""INSERT INTO verification_checks(run_id, decision_id, representation_a, representation_b,
                              outcome, detail, checked_at) VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                         (run, decision_id, stored.label, alt.label, outcome, json.dumps({"findings": len(findings)})))
            conn.executemany("""INSERT INTO verification_findings(run_id, decision_id, kind, severity, representation_a,
                                  representation_b, locator, value_a, value_b, detail, created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                             [(run, decision_id, f["kind"], f["severity"], stored.label, alt.label, f["locator"],
                               f["value_a"], f["value_b"], json.dumps(f["detail"], ensure_ascii=False))
                              for f in findings])
    conn.execute("UPDATE verification_runs SET finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), pairs=?, "
                 "agreeing=?, findings=?, errors=? WHERE id=?",
                 (counts["pairs"], counts["agreeing"], counts["findings"], counts["errors"], run))
    kinds_found = {f"{r[0]}/{r[1]}": r[2] for r in conn.execute(
        "SELECT kind, severity, count(*) FROM verification_findings WHERE run_id=? GROUP BY kind, severity", (run,))}
    return {"run_id": run, "planned": len(plan), **counts, "by_pair_kind": by_kind, "finding_kinds": kinds_found}
