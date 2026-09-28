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
"""


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
    body = "\n".join(t for _rn, section, t in paragraphs if section not in ("titel", "orientierungssatz"))
    return Representation(label, numbering, rn, body, case_numbers)


def stored_representation(conn: sqlite3.Connection, decision_id: str) -> Representation:
    d = conn.execute("SELECT source_id, numbering, case_numbers FROM decisions WHERE id=?", (decision_id,)).fetchone()
    rows = conn.execute("SELECT rn, section, text FROM paragraphs WHERE decision_id=? ORDER BY ordinal",
                        (decision_id,)).fetchall()
    return from_paragraphs(f"stored:{d['source_id']}", [tuple(r) for r in rows], d["numbering"],
                           json.loads(d["case_numbers"]))


def parsed_representation(label: str, parsed: ParsedDecision) -> Representation:
    paragraphs, numbering = assemble("X", parsed.sections)
    return from_paragraphs(label, [(p.rn, p.section, p.text) for p in paragraphs], numbering, parsed.case_numbers)


def compare(a: Representation, b: Representation) -> list[dict]:
    findings: list[dict] = []

    def add(kind, severity, locator=None, value_a=None, value_b=None, **detail):
        findings.append({"kind": kind, "severity": severity, "locator": locator,
                         "value_a": None if value_a is None else json.dumps(value_a, ensure_ascii=False),
                         "value_b": None if value_b is None else json.dumps(value_b, ensure_ascii=False),
                         "detail": detail})
    if {case_key(c) for c in a.case_numbers} != {case_key(c) for c in b.case_numbers}:
        add("case_numbers_mismatch", "major", None, a.case_numbers, b.case_numbers)
    if a.numbering != b.numbering:
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
                add("text_mismatch", "minor" if ratio >= 0.97 else "major", f"rn-{rn}", a.rn_texts[rn][:300],
                    b.rn_texts[rn][:300], similarity=round(ratio, 4),
                    hash_a=text_hash(a.rn_texts[rn]), hash_b=text_hash(b.rn_texts[rn]))
    else:
        ta, tb = normalise(a.body_text), normalise(b.body_text)
        if ta != tb:
            ratio = difflib.SequenceMatcher(None, ta, tb).quick_ratio()
            add("text_mismatch", "minor" if ratio >= 0.97 else "major", "body", None, None,
                similarity=round(ratio, 4), hash_a=text_hash(a.body_text), hash_b=text_hash(b.body_text))
    return findings


def _rn_key(label: str):
    m = re.match(r"(\d+)(.*)", label)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, label)


# --- NRWE PDF: an independent parser for the second representation --------------------------------

RIGHT_RN = re.compile(r"^(?P<text>.*?\S)?\s{3,}(?P<rn>\d{1,3})\s*$")
HEAD_FIELD = re.compile(r"^(Datum|Gericht|Spruchkörper|Entscheidungsart|Aktenzeichen|ECLI):\s+(.+)$")


def parse_nrwe_pdf(pdf: bytes, url: str, source_doc_id: str) -> ParsedDecision:
    from .sources.pdftext import join_lines, pdf_text
    text = pdf_text(pdf, url=url)
    lines = [line.rstrip() for line in text.replace("\f", "\n").split("\n")]
    fields: dict[str, str] = {}
    for line in lines[:60]:
        m = HEAD_FIELD.match(line.strip())
        if m and m.group(1) not in fields:
            fields[m.group(1)] = squash(m.group(2))
    if not fields.get("Aktenzeichen") or not fields.get("Datum"):
        raise UpstreamFailure("parse_error", "NRWE PDF head lacks Aktenzeichen/Datum", url=url)
    blocks: list[Block] = []
    current: list[str] = []
    label: str | None = None
    pending: str | None = None
    expected = 1
    started = False

    def flush():
        nonlocal current, label
        joined = join_lines(current)
        if joined:
            blocks.append(Block(text=joined, label=label, locator="nrwe-pdf"))
        current, label = [], None
    for line in lines:
        if not line.strip():
            flush()
            continue
        m = RIGHT_RN.match(line)
        if m and int(m.group("rn")) == expected:
            started = True
            flush()
            expected += 1
            if m.group("text"):
                label, current = m.group("rn"), [m.group("text")]
            else:
                pending = m.group("rn")
            continue
        if not started:
            continue
        if pending:
            flush()
            label, current, pending = pending, [line], None
            continue
        current.append(line)
    flush()
    if not blocks:
        raise UpstreamFailure("parse_error", "no Randnummern found in NRWE PDF", url=url)
    raw_az = fields["Aktenzeichen"]
    return ParsedDecision(source_id="nrw-pdf", source_doc_id=source_doc_id, source_url=url,
                          court_name=fields.get("Gericht", ""), decision_date=iso_date(fields["Datum"]),
                          case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
                          decision_type=fields.get("Entscheidungsart"),
                          sections=[Section("gruende", "court", "NRWE PDF body (right-margin Rn)", blocks)],
                          ecli=fields.get("ECLI"))


# --- alternates -------------------------------------------------------------------------------------

@dataclass
class Alternate:
    label: str
    load: Callable[[], ParsedDecision]
    network: bool


def alternates(conn: sqlite3.Connection, decision: sqlite3.Row, http=None) -> list[Alternate]:
    out: list[Alternate] = []
    from .store import read_capture
    # 1. offline: a second official source's capture of the same decision (skipped duplicate)
    for item in conn.execute("""SELECT * FROM source_items WHERE decision_id IS NULL AND state='skipped'
                                AND failure_kind='duplicate_identity' AND content_hash IS NOT NULL
                                AND failure_detail LIKE ?""", (decision["id"] + " %",)).fetchall():
        def load(item=item):
            from .sources import get_adapter
            ext = (conn.execute("SELECT extension FROM captures WHERE sha256=?", (item["content_hash"],)).fetchone()
                   or ["bin"])[0]
            body = read_capture(conn, item["content_hash"])
            return get_adapter(item["source_id"]).parse(Capture(body, item["url"], "", ext), item)
        out.append(Alternate(f"capture:{item['source_id']}", load, False))
    # 2. NRWE: the PDF of the same page
    if decision["source_id"] == "nrw" and http is not None:
        path = decision["source_url"].split("nrwe.justiz.nrw.de", 1)[-1]
        pdf_url = f"https://nrwe.justiz.nrw.de/pdfdownload/downloadEntscheidung.php?entscheidung=/nrwe{path}"

        def load_pdf():
            response = http.get(pdf_url, timeout=120)
            if not response.body.startswith(b"%PDF"):
                raise UpstreamFailure("unexpected_representation", "NRWE PDF download is not a PDF", url=pdf_url)
            return parse_nrwe_pdf(response.body, pdf_url, decision["source_doc_id"])
        out.append(Alternate("nrwe-pdf", load_pdf, True))
    # 3. RII XML <-> bverwg.de HTML for BVerwG decisions with an ECLI
    if decision["court_id"] == "bverwg" and decision["ecli"] and http is not None:
        suffix = decision["ecli"].rsplit(":", 1)[-1]
        if decision["source_id"] == "rii":
            def load_site():
                from .sources.bverwg import BverwgAdapter
                item = {"source_doc_id": suffix, "url": f"https://www.bverwg.de/de/{suffix}",
                        "listed_date": decision["decision_date"], "listed_case_numbers": None, "extra": {}}
                adapter = BverwgAdapter()
                return adapter.parse(adapter.fetch(http, item), item)
            out.append(Alternate("bverwg-html", load_site, True))
        elif decision["source_id"] == "bverwg":
            row = conn.execute("""SELECT * FROM source_items WHERE source_id='rii' AND listed_date=? AND
                                  listed_court='bverwg'""", (decision["decision_date"],)).fetchall()
            keys = {case_key(c) for c in json.loads(decision["case_numbers"])}
            match = [r for r in row if {case_key(c) for c in split_case_numbers(r["listed_case_numbers"] or "")} & keys]
            if len(match) == 1:
                def load_rii(item=match[0]):
                    from .sources.rii import RiiAdapter
                    adapter = RiiAdapter()
                    return adapter.parse(adapter.fetch(http, item), item)
                out.append(Alternate("rii-xml", load_rii, True))
    return out


def verify_sample(conn: sqlite3.Connection, *, sample: int, http=None, source: str | None = None,
                  seed: int | None = None) -> dict:
    conn.executescript(SCHEMA)
    run = conn.execute("INSERT INTO verification_runs(started_at, sample, verifier_version) "
                       "VALUES(strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), ?, ?)", (sample, VERIFIER_VERSION)).lastrowid
    # Candidates: decisions that have an alternate representation (duplicates first: they cost no request).
    dup_ids = [r[0] for r in conn.execute("""SELECT DISTINCT substr(failure_detail, 1, instr(failure_detail, ' ') - 1)
                                             FROM source_items WHERE state='skipped' AND content_hash IS NOT NULL
                                             AND failure_kind='duplicate_identity'""")]
    where = ["(source_id = 'nrw' OR court_id = 'bverwg')"] if http is not None else ["0"]
    params: list = []
    if source:
        where.append("source_id = ?")
        params.append(source)
    online = [r[0] for r in conn.execute(f"SELECT id FROM decisions WHERE {' AND '.join(where)}", params)]
    rng = random.Random(seed)
    rng.shuffle(online)
    candidates = list(dict.fromkeys(dup_ids + online))[:sample]
    counts = {"pairs": 0, "agreeing": 0, "findings": 0, "errors": 0}
    for decision_id in candidates:
        decision = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
        if decision is None:
            continue
        stored = stored_representation(conn, decision_id)
        for alt in alternates(conn, decision, http):
            counts["pairs"] += 1
            try:
                other = parsed_representation(alt.label, alt.load())
                findings = compare(stored, other)
                outcome = "disagree" if findings else "agree"
            except (UpstreamFailure, ValueError, OSError) as exc:
                findings = [{"kind": "representation_error", "severity": "minor", "locator": None, "value_a": None,
                             "value_b": None, "detail": {"error": f"{type(exc).__name__}: {exc}"[:400]}}]
                outcome = "error"
            counts["agreeing" if outcome == "agree" else ("errors" if outcome == "error" else "findings")] += 1
            conn.execute("""INSERT INTO verification_checks(run_id, decision_id, representation_a, representation_b,
                              outcome, detail, checked_at) VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                         (run, decision_id, stored.label, alt.label, outcome,
                          json.dumps({"findings": len(findings)})))
            conn.executemany("""INSERT INTO verification_findings(run_id, decision_id, kind, severity, representation_a,
                                  representation_b, locator, value_a, value_b, detail, created_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                             [(run, decision_id, f["kind"], f["severity"], stored.label, alt.label, f["locator"],
                               f["value_a"], f["value_b"], json.dumps(f["detail"], ensure_ascii=False))
                              for f in findings])
    conn.execute("UPDATE verification_runs SET finished_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), pairs=?, "
                 "agreeing=?, findings=?, errors=? WHERE id=?",
                 (counts["pairs"], counts["agreeing"], counts["findings"], counts["errors"], run))
    kinds = {r[0]: r[1] for r in conn.execute("SELECT kind, count(*) FROM verification_findings WHERE run_id=? "
                                               "GROUP BY kind", (run,))}
    return {"run_id": run, "candidates": len(candidates), **counts, "finding_kinds": kinds}
