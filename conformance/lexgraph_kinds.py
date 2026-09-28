"""Lexgraph conformance kinds (see conformance/README.md, section "Kinds").

Every kind is a thin adapter: it reads the case input, calls Lexgraph's
canonical implementation and projects the result onto plain JSON.  No legal
logic lives here, so a fix in ``api/`` or ``pipeline/`` shows up in every case.

Only modules that import without FastAPI are used (``api.law_structure``,
``api.legislative_materials``, ``api.page_furniture``, ``api.norm_identity``,
``pipeline.fetch_gii_toc`` and the BGBl command parser), so the suite runs on a
plain Python with the pipeline requirements.
"""
from __future__ import annotations

import hashlib
import json
import sys
from typing import Any

from conformance.runner import ROOT, read_input

from api.law_structure import iter_units, parse_norm_structure, sentence_units
from api.legislative_materials import segment_drucksache
from api.norm_identity import gii_norm_url, load_gii_toc
from api.page_furniture import strip_page_furniture


# ----------------------------------------------------------------- inputs

def _json(path: str) -> Any:
    return json.loads(read_input(path).decode("utf-8"))


def _row(inp: dict[str, Any]) -> Any:
    """``fixture`` (JSON file) narrowed by ``key`` (object member) or
    ``where`` (first list row whose fields equal the given values)."""
    data = _json(inp["fixture"])
    if "key" in inp:
        return data[inp["key"]]
    if "where" in inp:
        rows = data.values() if isinstance(data, dict) else data
        return next(row for row in rows if isinstance(row, dict) and all(
            row.get(k) == v for k, v in inp["where"].items()))
    return data


def _text(inp: dict[str, Any], default_field: str) -> str:
    if "text" in inp:
        return inp["text"]
    return _row(inp)[inp.get("field", default_field)]


# ----------------------------------------------------------------- kinds

def norm_structure(inp: dict[str, Any]) -> dict[str, Any]:
    """Legal units of one canonical norm body (api.law_structure)."""
    text = _text(inp, "text")
    options = {"bavarian": bool(inp.get("bavarian")),
               "annex": bool(inp.get("annex"))}
    structure = parse_norm_structure(text, **options)
    units = list(iter_units(structure))
    locators: dict[str, list[str]] = {}
    for unit in units:
        locators.setdefault(unit["type"], []).append(unit["locator"])
    return {
        "parser_version": structure["parser_version"],
        "mode": structure["mode"],
        "numbered_absaetze": structure["numbered_absaetze"],
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "counts": structure["counts"],
        "locators": locators,
        "texts": {unit["locator"]: unit["text"] for unit in units},
        "flags": {unit["locator"]: unit["flags"] for unit in units
                  if unit.get("flags")},
        "sentences": [{"locator": row["locator"],
                       "items": [item["short_locator"] for item in row["items"]]}
                      for row in sentence_units(text, **options)],
    }


def _pages(directory: str) -> tuple[dict[str, Any], list[str | None]]:
    meta = _json(f"{directory}/meta.json")
    pages: list[str | None] = [None] * int(meta["page_count"])
    for number, row in meta["pages"].items():
        pages[int(number) - 1] = read_input(
            f"{directory}/{row['file']}").decode("utf-8")
    return meta, pages


def material_fragments(inp: dict[str, Any]) -> dict[str, Any]:
    """Segment a Drucksache fixture (api.legislative_materials).

    Pages not listed in the fixture's ``meta.json`` are passed as ``None``
    (not extracted).  ``act_slugs`` and the document entry come from
    ``sources`` (Lexgraph's ``data/materials_sources.json``); a Drucksache
    without an entry there uses ``meta.json``'s ``document_meta``.
    """
    meta, pages = _pages(inp["fixture"])
    drucksache = inp["drucksache"]
    sources = _json(inp.get("sources", "data/materials_sources.json"))
    document_meta = next((dict(row) for row in sources.get("documents") or []
                          if row.get("drucksache") == drucksache),
                         meta.get("document_meta") or {})
    result = segment_drucksache(pages, drucksache=drucksache,
                                document_meta=document_meta,
                                act_slugs=sources.get("act_slugs") or {})
    document = result["document"]
    prefix = document["id"] + ":"

    def source_slice(span: dict[str, Any]) -> str:
        return (pages[span["pdf_page"] - 1] or "")[span["start"]:span["end"]]

    fragments = {}
    for row in result["fragments"]:
        fragments[row["id"].removeprefix(prefix)] = {
            "role": row["role"],
            "section_path": row["section_path"],
            "heading": row["heading"],
            "heading_source": source_slice(row["heading_span"]),
            "body": row["body"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "complete": row["complete"],
            "target_norms": [t["canonical_id"] for t in row["target_norms"]],
            "mentions": [m["canonical_id"] for m in row["mentions"]],
            "relations": [{"type": r["type"],
                           "target": r["target"]["canonical_id"],
                           "derived_from": r["evidence"]["derived_from"],
                           "confidence": r["evidence"]["confidence"],
                           "legal_effect_verified":
                               r["evidence"]["legal_effect_verified"]}
                          for r in row["relations"]],
            "government_position": row.get("government_position"),
            "notes": row["notes"],
        }
    extraction = document["extraction"]
    return {
        "document": {
            "id": document["id"],
            "parser_version": document["parser_version"],
            "extracted_pages": [p["pdf_page"] for p in extraction["pages"]],
            "pages_missing_count": len(extraction["pages_missing"]),
            "warnings": document["warnings"],
        },
        "sections": {s["role"]: s["pdf_page_start"] for s in result["sections"]},
        "fragment_ids": list(fragments),
        "fragments": fragments,
    }


def norm_url(inp: dict[str, Any]) -> dict[str, Any]:
    """Official GII per-norm URL through the captured table of contents
    (pipeline.fetch_gii_toc.parse_toc + api.norm_identity.gii_norm_url).

    ``toc_html``: a captured GII ``index.html`` of act ``slug``;
    ``toc_json``: a captured TOC file as Lexgraph serves it; neither (or
    null): no TOC is available.
    """
    slug = inp["slug"]
    if inp.get("toc_html"):
        from pipeline.fetch_gii_toc import parse_toc
        raw = read_input(inp["toc_html"])
        toc = {"acts": {slug: {"norms": parse_toc(raw),
                               "fetched_at": inp.get("fetched_at"),
                               "sha256": hashlib.sha256(raw).hexdigest()}}}
    elif inp.get("toc_json"):
        toc = load_gii_toc(ROOT / inp["toc_json"])
    else:
        toc = None
    results = []
    for query in inp["queries"]:
        row = gii_norm_url(toc, slug, query["unit"], query["number"])
        results.append({"key": f"{query['unit']}/{query['number']}",
                        "url": row["url"], "act_url": row["act_url"],
                        "status": row["status"]})
    entry = ((toc or {}).get("acts") or {}).get(slug)
    return {"results": results,
            "toc_norms": entry.get("norms") if isinstance(entry, dict) else None}


def page_furniture(inp: dict[str, Any]) -> dict[str, Any]:
    """Printed page furniture in extracted official text
    (api.page_furniture.strip_page_furniture)."""
    clean, spans = strip_page_furniture(_text(inp, "raw"))
    return {"spans": spans, "clean": clean}


def amendment_commands(inp: dict[str, Any]) -> dict[str, Any]:
    """Amendment commands of one final BGBl article
    (pipeline/backfill_bgbl_history.parse_amendment_commands)."""
    pipeline = str(ROOT / "pipeline")
    if pipeline not in sys.path:
        sys.path.insert(0, pipeline)
    from backfill_bgbl_history import parse_amendment_commands

    row = _row(inp)
    commands, norms = parse_amendment_commands({
        "article": row["article"], "heading": row["heading"],
        "text": row["text"]})
    return {
        "command_count": len(commands),
        "norms": norms,
        "commands": {str(c["item"]): {"operation": c["operation"],
                                      "ref": c["ref"], "raw": c["raw"],
                                      "parent_scope": c.get("parent_scope")}
                     for c in commands},
    }


KINDS = {
    "norm_structure": norm_structure,
    "material_fragments": material_fragments,
    "norm_url": norm_url,
    "page_furniture": page_furniture,
    "amendment_commands": amendment_commands,
}
