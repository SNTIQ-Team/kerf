"""Courts conformance kinds: each calls the courts package's canonical code."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path

from conformance.runner import ROOT, read_input

SNAPSHOT = ROOT / "conformance" / "fixtures" / "lexgraph_data"


@contextlib.contextmanager
def lexgraph_snapshot():
    """Norm resolution against the committed Lexgraph act index + GII catalogue snapshot."""
    from courts.acts import resolver
    previous = os.environ.get("LEXGRAPH_DATA_DIR")
    os.environ["LEXGRAPH_DATA_DIR"] = str(SNAPSHOT)
    resolver.cache_clear()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("LEXGRAPH_DATA_DIR", None)
        else:
            os.environ["LEXGRAPH_DATA_DIR"] = previous
        resolver.cache_clear()


MEDIA = {"zip": "application/zip", "html": "text/html", "pdf": "application/pdf", "json": "application/json",
         "xml": "application/xml"}


def _parsed(inp: dict):
    from courts.sources import get_adapter
    from courts.sources.base import Capture
    from courts.store import finalise
    fixture = inp["fixture"]
    ext = Path(fixture.removesuffix(".gz")).suffix.lstrip(".") or "bin"
    body = read_input(fixture)
    capture = Capture(body, inp.get("url") or inp["item"].get("url", ""), MEDIA.get(ext, ""), ext)
    item = {"listed_date": None, "listed_court": None, "listed_case_numbers": None, "extra": {}, **inp["item"]}
    with lexgraph_snapshot():
        return finalise(get_adapter(inp["adapter"]).parse(capture, item))


def _paragraphs(parsed):
    from courts.paragraphs import assemble
    from courts.store import decision_identity
    return assemble(decision_identity(parsed), parsed.sections)


def paragraphs_from_capture(inp: dict) -> dict:
    parsed = _parsed(inp)
    paras, numbering = _paragraphs(parsed)
    return {"numbering": numbering, "paragraph_ids": [p.id.split("#", 1)[1] for p in paras],
            "rn_blocks": {p.rn: len(p.blocks) for p in paras if p.rn},
            "sections": [{"name": s.name, "authorship": s.authorship} for s in parsed.sections]}


def quote_to_paragraph(inp: dict) -> dict:
    from courts.paragraphs import locate_quote
    paras, _ = _paragraphs(_parsed(inp))
    return {"paragraph_ids": [pid.split("#", 1)[1] for pid in locate_quote(paras, inp["quote"])]}


def decision_metadata(inp: dict) -> dict:
    from courts.store import decision_identity
    d = _parsed(inp)
    return {"id": decision_identity(d), "ecli": d.ecli, "court_id": d.court_id, "decision_date": d.decision_date,
            "decision_type": d.decision_type, "case_numbers_raw": d.case_numbers_raw, "case_numbers": d.case_numbers,
            "numbering": _paragraphs(d)[1]}


def case_numbers(inp: dict) -> dict:
    from courts.casenumbers import split_case_numbers
    return {"case_numbers": split_case_numbers(inp["raw"])}


def page_classification(inp: dict) -> dict:
    from courts.detect import classify_response
    verdict = classify_response(read_input(inp["fixture"]), status=inp.get("status"),
                                headers=inp.get("headers") or {"content-type": "text/html"})
    return {"kind": verdict[0] if verdict else None}


def norm_citations(inp: dict) -> dict:
    from courts.citations import extract_norms
    with lexgraph_snapshot():
        rels = extract_norms(inp["text"], "p", land=inp.get("land"))
    return {"targets": [r.target_id for r in rels], "target_set": sorted({r.target_id or "" for r in rels}),
            "relations": [r.relation for r in rels]}


def decision_citations(inp: dict) -> dict:
    from courts.casenumbers import case_key
    from courts.citations import extract_decisions
    rels = extract_decisions(inp["text"], "p", own_court_id=inp.get("own_court_id"),
                             own_case_keys={case_key(c) for c in inp.get("own_case_numbers", [])},
                             own_land=inp.get("land"))
    return {"citations": [{"relation": r.relation, "target_id": r.target_id} for r in rels],
            "target_set": sorted({r.target_id for r in rels})}


def relation_assessments(inp: dict) -> dict:
    from courts.citations import extract_all
    from courts.polarity import assess
    with lexgraph_snapshot():
        rels = extract_all(inp["text"], "p", own_court_id=inp.get("own_court_id"), own_land=inp.get("land"))
    assessments = []
    for r in rels:
        for p in assess(inp["text"], (r.span_start, r.span_end), target_kind=r.target_kind, target_id=r.target_id,
                        own_court_id=inp.get("own_court_id"), jurisdiction=inp.get("jurisdiction"), detail=r.detail):
            assessments.append({"assessment": p.assessment, "target_id": r.target_id, "rule_id": p.rule.rule_id})
    return {"relations": [{"relation": r.relation, "target_id": r.target_id} for r in rels],
            "assessments": assessments,
            "assessment_keys": sorted({f"{a['assessment']}|{a['target_id']}" for a in assessments})}


def act_resolution(inp: dict) -> dict:
    from courts.acts import resolve_act
    with lexgraph_snapshot():
        identity = resolve_act(inp["abbreviation"], land=inp.get("land"))
    return {"prefix": identity.prefix, "basis": identity.basis, "in_corpus": identity.in_corpus,
            "act_id": identity.act_id}


def cross_representation(inp: dict) -> dict:
    """The stored representation (adapter parse of the capture, through the model) against an independent
    second representation of the same decision (e.g. the NRWE PDF), as `courts verify` compares them."""
    from courts.verify import compare, from_paragraphs, parse_nrwe_pdf, parsed_representation
    parsed = _parsed(inp)
    paras, numbering = _paragraphs(parsed)
    stored = from_paragraphs("stored", [(p.rn, p.section, p.text) for p in paras], numbering, parsed.case_numbers)
    other_kind = inp["other"]["kind"]
    if other_kind != "nrwe-pdf":
        raise ValueError(f"unsupported representation {other_kind}")
    other = parse_nrwe_pdf(read_input(inp["other"]["fixture"]), inp["other"].get("url", ""), "x")
    rep = parsed_representation(other_kind, other)
    findings = compare(stored, rep)
    return {"numbering": [stored.numbering, rep.numbering],
            "rn_labels_equal": list(stored.rn_texts) == list(rep.rn_texts),
            "rn_count": [len(stored.rn_texts), len(rep.rn_texts)],
            "rn_without_own_text": [parsed.metadata.get("rn_without_own_text"),
                                    other.metadata.get("rn_without_own_text")],
            "major_findings": [f"{f['kind']}@{f['locator']}" for f in findings if f["severity"] == "major"],
            "rn_texts_other": {rn: rep.rn_texts.get(rn, "")[:60] for rn in inp.get("show_rn", [])}}


def assessment_attachment(inp: dict) -> dict:
    """Which decisions' relations carry a user (corrections-ledger) assessment, as the read API reports them.

    Builds a scratch corpus from minimal decisions, records the user rows through the ledger, then reads
    through courts.read (citing_decisions, with and without the assessment filter)."""
    import sqlite3
    from courts.ledger import record
    from courts.model import Block, ParsedDecision, Section
    from courts.read import citing_decisions
    from courts.store import init_db, store_decision
    from courts.sync import after_store
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    with lexgraph_snapshot():
        for d in inp["decisions"]:
            parsed = ParsedDecision(
                source_id="rii", source_doc_id=d["doc"], source_url=f"https://example.invalid/{d['doc']}",
                court_name=d["court_name"], decision_date=d["date"], case_numbers_raw=d["case_number"],
                case_numbers=[d["case_number"]], decision_type="Beschluss", ecli=d.get("ecli"),
                metadata={"norms": d.get("metadata_norms", [])},
                sections=[Section("gruende", "court", "fixture", [Block(t, str(i)) for i, t in
                                                                  enumerate(d["paragraphs"], 1)])])
            decision_id, _ = store_decision(conn, parsed, content_hash=d["doc"], capture_path="-",
                                            media_type="text/html")
            after_store(conn, decision_id)
        for row in inp["user_rows"]:
            record(conn, target=row["target"], kind="relation_assessment", target_id=row["target_id"],
                   new={"assessment": row["assessment"], "status": "confirmed"}, evidence="conformance",
                   author="conformance", write_fixture=False)
    out = citing_decisions(conn, inp["query"], limit=100)
    filtered = citing_decisions(conn, inp["query"], assessment=inp["assessment"], limit=100)
    user = {}
    for r in out["relations"]:
        user.setdefault(r["source_id"], []).extend(
            f"{a['assessment']}@{(r['evidence']['paragraph_id'] or '#metadata').split('#', 1)[1]}"
            for a in r["assessments"] if a["derived_by"] == "user")
    return {"user_assessments": {k: sorted(v) for k, v in user.items()},
            "filtered_decisions": sorted({r["source_id"] for r in filtered["relations"]})}


KINDS = {f.__name__: f for f in (paragraphs_from_capture, cross_representation, assessment_attachment, quote_to_paragraph, decision_metadata, case_numbers,
                                 page_classification, norm_citations, decision_citations, relation_assessments,
                                 act_resolution)}
