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


KINDS = {f.__name__: f for f in (paragraphs_from_capture, quote_to_paragraph, decision_metadata, case_numbers,
                                 page_classification, norm_citations, decision_citations, relation_assessments,
                                 act_resolution)}
