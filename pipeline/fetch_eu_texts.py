"""Fetch German EU legal texts article by article from the Publications Office.

Source: CELLAR, the Publications Office's machine channel (``publications.europa.eu``;
robots ``Allow: /`` for ``/resource`` and ``/webapi/rdf/sparql``; reuse under Decision
2011/833/EU, EUR-Lex legal notice).  The EUR-Lex website itself sits behind a WAF
challenge and is never fetched (docs/SOURCES.md).

Per act (``CORPUS`` plus discovered extension decisions of Implementing Decision
2022/382):

* SPARQL metadata of the work: adoption (``work_date_document``), signature, every
  ``resource_legal_date_entry-into-force`` with the asserted role and scope of each date, end of validity,
  the Official Journal issue and date, the
  German title, corrigenda (``resource_legal_corrects_resource_legal``), repealing and
  amending acts, and the consolidated versions (``act_consolidated_*``, CELEX sector 0).
* Text editions: the Official Journal text (sector 3) and every consolidated version,
  Formex 4 (``fmx4``) preferred, XHTML/HTML otherwise; treaties and the Charter come as
  one work per article (``12016E078`` = Art. 78 AEUV).

Every fetched byte string is cached under ``data/cache/eu_texts/`` with its SHA-256 and
first retrieval time (immutable publications: a cached item is never refetched).  An
edition whose bytes do not parse into articles is dropped with a warning, never
guessed.

Article text is rendered one Unterabsatz per line: ``(1) …`` opens an Absatz, a further
paragraph of the same Absatz is a new line, list points (``a)``, ``1.``, ``—``) stay on
the line of their lead-in.  ``api.law_structure.parse_norm_structure(eu=True)`` reads
this shape.  Annexes, footnotes and recitals are not norm text and are left out
(``annexes`` counts what was skipped).

Output (``data/snapshots/eu_texts/<date>/acts.jsonl``): one row per act with its
metadata, relations and ``editions`` (``articles``: ``enbez``/``titel``/``text``).

Usage:
    python3 pipeline/fetch_eu_texts.py [--acts 32016R0679,12016E] [--offline]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

if __package__:
    from .eu_dates import general_date
else:
    from eu_dates import general_date

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "cache" / "eu_texts"
SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR = "https://publications.europa.eu/resource/cellar/"
CELEX_URI = "http://publications.europa.eu/resource/celex/"
UA = "SNTIQ-kerf/0.1 (research; +https://sntiq.com/)"
DEU = "http://publications.europa.eu/resource/authority/language/DEU"
PREFIXES = """PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
PREFIX owl: <http://www.w3.org/2002/07/owl#>
"""

# Curated German-language corpus of EU legislation and treaties.  ``aliases``
# are official or customary German short forms; the canonical EU_ACTS map in
# api.norm_identity holds the ones courts use.
CORPUS: list[dict[str, Any]] = [
    {"celex": "32016R0679", "jurabk": "DSGVO", "family": "regulation",
     "aliases": ["DS-GVO", "GDPR", "EU-DSGVO", "Datenschutz-Grundverordnung",
                 "VO (EU) 2016/679"]},
    {"celex": "32001L0055", "jurabk": "RL 2001/55/EG", "family": "directive",
     "aliases": ["Massenzustrom-Richtlinie", "Massenzustromrichtlinie",
                 "Temporary-Protection-Richtlinie", "Richtlinie 2001/55/EG"]},
    {"celex": "32022D0382", "jurabk": "DB (EU) 2022/382", "family": "decision",
     "aliases": ["Durchführungsbeschluss (EU) 2022/382"]},
    {"celex": "32013R0604", "jurabk": "Dublin-III-VO", "family": "regulation",
     "aliases": ["Dublin III", "Dublin-III-Verordnung", "VO (EU) Nr. 604/2013"]},
    {"celex": "32013L0033", "jurabk": "Aufnahme-RL", "family": "directive",
     "aliases": ["Aufnahmerichtlinie", "RL 2013/33/EU"]},
    {"celex": "32013L0032", "jurabk": "Verfahrens-RL", "family": "directive",
     "aliases": ["Asylverfahrensrichtlinie", "Verfahrensrichtlinie", "RL 2013/32/EU"]},
    {"celex": "32011L0095", "jurabk": "Qualifikations-RL", "family": "directive",
     "aliases": ["Qualifikationsrichtlinie", "QRL", "RL 2011/95/EU"]},
    {"celex": "12016P", "jurabk": "GRCh", "family": "charter", "articles": 54,
     "aliases": ["GRC", "EU-GRCh", "Grundrechtecharta", "Charta der Grundrechte"],
     # Cellar dates each Charter article's entry into force with the Lisbon date
     # (2009-12-01); the 2016 text republishes the adapted Charter of 12.12.2007.
     "valid_from_policy": "entry_into_force"},
    {"celex": "12016E", "jurabk": "AEUV", "family": "treaty", "articles": 358,
     "base": "12008E", "aliases": []},
    {"celex": "12016M", "jurabk": "EUV", "family": "treaty", "articles": 55,
     "base": "12008M", "aliases": []},
    {"celex": "32004R0883", "jurabk": "VO (EG) Nr. 883/2004", "family": "regulation",
     "aliases": ["Grundverordnung zur Koordinierung der Systeme der sozialen Sicherheit"]},
    {"celex": "32009R0987", "jurabk": "VO (EG) Nr. 987/2009", "family": "regulation",
     "aliases": []},
    {"celex": "32004L0038", "jurabk": "RL 2004/38/EG", "family": "directive",
     "aliases": ["Freizügigkeitsrichtlinie", "Unionsbürgerrichtlinie"]},
    {"celex": "32003L0109", "jurabk": "RL 2003/109/EG", "family": "directive",
     "aliases": ["Daueraufenthaltsrichtlinie"]},
    {"celex": "32003L0086", "jurabk": "RL 2003/86/EG", "family": "directive",
     "aliases": ["Familienzusammenführungsrichtlinie"]},
    {"celex": "32008L0115", "jurabk": "RL 2008/115/EG", "family": "directive",
     "aliases": ["Rückführungsrichtlinie"]},
    {"celex": "31996L0071", "jurabk": "RL 96/71/EG", "family": "directive",
     "aliases": ["Entsenderichtlinie"]},
    {"celex": "32011L0083", "jurabk": "RL 2011/83/EU", "family": "directive",
     "aliases": ["Verbraucherrechterichtlinie"]},
    # The ten legislative acts in the 2024 Pact on Migration and Asylum.
    {"celex": "32024L1346", "jurabk": "RL (EU) 2024/1346", "family": "directive"},
    *[{"celex": f"32024R{number}", "jurabk": f"VO (EU) 2024/{number}", "family": "regulation"}
      for number in (1347, 1348, 1349, 1350, 1351, 1352, 1356, 1358, 1359)],
    {"celex": "32016L1919", "jurabk": "RL (EU) 2016/1919", "family": "directive"},
    {"celex": "32022R1925", "jurabk": "VO (EU) 2022/1925", "family": "regulation",
     "aliases": ["Digital Markets Act", "DMA", "Gesetz über digitale Märkte"]},
    {"celex": "32024R1689", "jurabk": "VO (EU) 2024/1689", "family": "regulation",
     "aliases": ["AI Act", "KI-Verordnung", "Verordnung über künstliche Intelligenz"]},
]
# Treaty consolidations are dated by the Treaty of Lisbon's entry into force (Cellar
# metadata of 12007L/TXT); the per-article works carry the original treaty's date.
LISBON = "12007L/TXT"
EXTENDED_ACT = "32022D0382"


# ------------------------------------------------------------------ network

class Cellar:
    def __init__(self, *, offline: bool = False, delay: float = 0.6, cache: Path | None = None):
        import requests
        self.cache = CACHE if cache is None else cache
        self.offline = offline
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._last = 0.0
        self.requests = 0

    def _wait(self) -> None:
        pause = self.delay - (time.time() - self._last)
        if pause > 0:
            time.sleep(pause)
        self._last = time.time()

    def sparql(self, query: str) -> list[dict[str, str]]:
        if self.offline:
            raise RuntimeError("offline: SPARQL unavailable")
        for attempt in range(3):
            self._wait()
            self.requests += 1
            response = self.session.get(SPARQL, params={
                "query": PREFIXES + query,
                "format": "application/sparql-results+json"}, timeout=120)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(5 * (attempt + 1))
                continue
            response.raise_for_status()
            return [{k: v["value"] for k, v in row.items()}
                    for row in response.json()["results"]["bindings"]]
        raise RuntimeError(f"SPARQL failed after retries ({response.status_code})")

    def get(self, url: str) -> dict[str, Any]:
        """Bytes of one Cellar resource (cache first) with provenance."""
        key = re.sub(r"[^A-Za-z0-9._-]+", "_", url.removeprefix(CELLAR))
        path, meta_path = self.cache / f"{key}.gz", self.cache / f"{key}.json"
        if path.is_file() and meta_path.is_file():
            data = gzip.decompress(path.read_bytes())
            meta = json.loads(meta_path.read_text())
            if hashlib.sha256(data).hexdigest() == meta.get("sha256"):
                return {"bytes": data, **meta, "cached": True}
        if self.offline:
            raise RuntimeError(f"offline: {url} not cached")
        for attempt in range(3):
            self._wait()
            self.requests += 1
            response = self.session.get(url, timeout=120)
            if response.status_code in {429, 500, 502, 503, 504}:
                time.sleep(5 * (attempt + 1))
                continue
            break
        response.raise_for_status()
        kind = response.headers.get("content-type", "")
        if "text/html" in kind and b"challenge" in response.content[:4000].lower():
            raise RuntimeError(f"{url}: challenge page, not a publication")
        data = response.content
        meta = {"url": url, "final_url": response.url, "content_type": kind,
                "sha256": hashlib.sha256(data).hexdigest(), "bytes_len": len(data),
                "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        self.cache.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(data, 9, mtime=0))
        meta_path.write_text(json.dumps(meta, sort_keys=True))
        return {"bytes": data, **meta, "cached": False}


def celex_uri(celex: str) -> str:
    return f"<{CELEX_URI}{quote(celex, safe='()')}>"


# ----------------------------------------------------------------- metadata

def work_metadata(cellar: Cellar, celex: str) -> dict[str, Any]:
    rows = cellar.sparql(f"""PREFIX ann: <http://publications.europa.eu/ontology/annotation#>
      SELECT ?p ?o ?dateRole ?dateComment WHERE {{ ?w owl:sameAs {celex_uri(celex)} . ?w ?p ?o .
      FILTER(?p IN (cdm:work_date_document, cdm:resource_legal_date_signature,
        cdm:resource_legal_date_entry-into-force, cdm:resource_legal_date_end-of-validity,
        cdm:resource_legal_eli, cdm:resource_legal_in-force,
        cdm:official-journal-act_date_publication, cdm:official-journal-act_number,
        cdm:official-journal-act_year, cdm:official-journal-act_part_of_collection_document))
      OPTIONAL {{ ?assertion owl:annotatedSource ?w ; owl:annotatedProperty ?p ; owl:annotatedTarget ?o .
        OPTIONAL {{ ?assertion ann:type_of_date ?dateRole }}
        OPTIONAL {{ ?assertion ann:comment_on_date ?dateComment }}
      }} }}""")
    values: dict[str, list[str]] = {}
    for row in rows:
        values.setdefault(row["p"].rsplit("#", 1)[-1], []).append(row["o"])
    oj = cellar.sparql(f"""SELECT ?d ?n ?y ?c WHERE {{ ?w owl:sameAs {celex_uri(celex)} .
      ?w cdm:resource_legal_published_in_official-journal ?oj .
      OPTIONAL {{ ?oj cdm:publication_general_date_publication ?d }}
      OPTIONAL {{ ?oj cdm:official-journal_number ?n }} OPTIONAL {{ ?oj cdm:official-journal_year ?y }}
      OPTIONAL {{ ?oj cdm:official-journal_part_of_collection_document ?c }} }}""")
    title = cellar.sparql(f"""SELECT ?t WHERE {{ ?w owl:sameAs {celex_uri(celex)} .
      ?e cdm:expression_belongs_to_work ?w ; cdm:expression_uses_language <{DEU}> ;
         cdm:expression_title ?t }} LIMIT 1""")
    eif = sorted(set(values.get("resource_legal_date_entry-into-force", [])))

    def date_rows(predicate: str) -> list[dict]:
        return [{"date": r["o"], "dateRole": r.get("dateRole", ""), "dateComment": r.get("dateComment", "")}
                for r in rows if r["p"].endswith("#" + predicate)]

    entry = date_rows("resource_legal_date_entry-into-force")
    end = date_rows("resource_legal_date_end-of-validity")
    oj_row = oj[0] if oj else {}
    act_by_act = (values.get("official-journal-act_date_publication") or [None])[0]
    published = act_by_act or oj_row.get("d")
    series = ((oj_row.get("c") or (values.get(
        "official-journal-act_part_of_collection_document") or [""])[0]).rsplit("/", 1)[-1]
        .replace("OJ-", "") or None)
    oj_ref = None
    if published and series:
        day = datetime.fromisoformat(published)
        if oj_row.get("n"):
            oj_ref = f"ABl. {series} {oj_row['n']} vom {day.day}.{day.month}.{day.year}"
        elif act_by_act and values.get("official-journal-act_number"):
            # act-by-act Official Journal (since 10/2023): "ABl. L, 2026/1912, 4.8.2026"
            oj_ref = (f"ABl. {series}, {values['official-journal-act_year'][0]}/"
                      f"{values['official-journal-act_number'][0]}, {day.day}.{day.month}.{day.year}")
        else:
            oj_ref = f"ABl. {series} vom {day.day}.{day.month}.{day.year}"
    return {
        "celex": celex,
        "title": (title[0]["t"] if title else None),
        "adopted": min(values.get("work_date_document", []) or [""]) or None,
        "signed": min(values.get("resource_legal_date_signature", []) or [""]) or None,
        "published": published, "oj_ref": oj_ref,
        "in_force": general_date(entry, "EV"),
        "applies_from": general_date(entry, "MA"),
        "entry_into_force_dates": eif,
        "end_of_validity": general_date(end, None),
        "eli": (values.get("resource_legal_eli") or [None])[0],
    }


def related(cellar: Cellar, celex: str) -> dict[str, list[dict[str, Any]]]:
    rows = cellar.sparql(f"""SELECT ?c ?rel WHERE {{ ?w owl:sameAs {celex_uri(celex)} .
      ?x ?rel ?w . ?x cdm:resource_legal_id_celex ?c .
      FILTER(?rel IN (cdm:resource_legal_corrects_resource_legal,
        cdm:resource_legal_repeals_resource_legal, cdm:resource_legal_implicitly_repeals_resource_legal,
        cdm:resource_legal_amends_resource_legal)) }}""")
    out: dict[str, list[dict[str, Any]]] = {"corrected_by": [], "repealed_by": [],
                                            "implicitly_repealed_by": [], "amended_by": []}
    names = {"corrects": "corrected_by", "implicitly_repeals": "implicitly_repealed_by",
             "repeals": "repealed_by", "amends": "amended_by"}
    for row in rows:
        rel = row["rel"].rsplit("#", 1)[-1].removeprefix("resource_legal_").removesuffix(
            "_resource_legal")
        bucket = names.get(rel)
        if bucket and all(r["celex"] != row["c"] for r in out[bucket]):
            out[bucket].append({"celex": row["c"]})
    for bucket in out.values():
        for row in bucket:
            meta = work_metadata(cellar, row["celex"])
            row.update({k: meta.get(k) for k in ("title", "adopted", "published", "oj_ref",
                                                 "in_force", "applies_from")})
        bucket.sort(key=lambda r: (r.get("published") or "", r["celex"]))
    # a corrigendum concerns only the language versions it is published in
    german = manifestations(cellar, [r["celex"] for r in out["corrected_by"]]) \
        if out["corrected_by"] else {}
    for row in out["corrected_by"]:
        row["german"] = bool(german.get(row["celex"]))
    return out


def consolidations(cellar: Cellar, celex: str) -> list[dict[str, str]]:
    rows = cellar.sparql(f"""SELECT ?c ?d WHERE {{ ?w owl:sameAs {celex_uri(celex)} .
      ?x cdm:act_consolidated_consolidates_resource_legal ?w ; cdm:resource_legal_id_celex ?c .
      OPTIONAL {{ ?x cdm:act_consolidated_date ?d }} }}""")
    wanted = re.compile(rf"0{re.escape(celex[1:])}-\d{{8}}")
    return sorted(({"celex": r["c"], "date": r.get("d") or f"{r['c'][-8:-4]}-{r['c'][-4:-2]}-{r['c'][-2:]}"}
                   for r in rows if wanted.fullmatch(r["c"])), key=lambda r: r["date"])


def manifestations(cellar: Cellar, celexes: list[str]) -> dict[str, dict[str, str]]:
    """celex -> {manifestation type: manifestation uri} for the German expression."""
    out: dict[str, dict[str, str]] = {}
    for start in range(0, len(celexes), 40):
        batch = celexes[start:start + 40]
        values = " ".join(celex_uri(c) for c in batch)
        rows = cellar.sparql(f"""SELECT ?s ?m ?t WHERE {{ VALUES ?s {{ {values} }} ?w owl:sameAs ?s .
          ?e cdm:expression_belongs_to_work ?w ; cdm:expression_uses_language <{DEU}> .
          ?m cdm:manifestation_manifests_expression ?e ; cdm:manifestation_type ?t }}""")
        for row in rows:
            celex = row["s"].removeprefix(CELEX_URI).replace("%2F", "/")
            out.setdefault(celex, {})[row["t"]] = row["m"]
    return out


def extension_decisions(cellar: Cellar) -> list[str]:
    """Acts that extend the validity of Implementing Decision 2022/382 (CELLAR relation)."""
    rows = cellar.sparql(f"""SELECT DISTINCT ?c WHERE {{ ?w owl:sameAs {celex_uri(EXTENDED_ACT)} .
      ?x cdm:resource_legal_extends_validity_of_resource_legal ?w ; cdm:resource_legal_id_celex ?c }}""")
    return sorted(r["c"] for r in rows if re.fullmatch(r"3\d{4}D\d{4}", r["c"]))


def manifestation_items(cellar: Cellar, manifestation: str) -> list[str]:
    """Content item URLs of one manifestation, in document order (not the .doc.xml)."""
    listing = cellar.get(manifestation.replace("http://", "https://"))
    text = listing["bytes"].decode("utf-8", "replace")
    items = []
    for match in re.finditer(r'rdf:about="([^"]+/(DOC_\d+))"(.*?)</rdf:Description>', text, re.S):
        same = re.findall(r'sameAs rdf:resource="([^"]+)"', match.group(3))
        if any(s.endswith(".doc.xml") for s in same):
            continue
        items.append((int(match.group(2)[4:]), match.group(1)))
    return [url.replace("http://", "https://") for _, url in sorted(set(items))]


# ------------------------------------------------------------------ parsing

_WS = re.compile(r"\s+")
_ARTICLE_LABEL = re.compile(r"^Artikel\s+(\d+[a-z]?)\s*$")


def _clean(value: str) -> str:
    return _WS.sub(" ", value or "").strip()


def _formex_text(el: ET.Element) -> str:
    """Flatten Formex inline content (tails of ``el`` itself excluded).

    Quoted articles retain their headings and paragraph/list numbers. The
    enclosing article renders its own numbers separately and omits footnotes.
    """
    parts: list[str] = []

    def walk(node: ET.Element, quoted_article: bool = False) -> None:
        quoted_article = quoted_article or node.tag == "ARTICLE"
        if node.tag == "NOTE" or (not quoted_article and node.tag in {"NO.P", "NO.PARAG"}):
            return
        if node.tag in {"QUOT.START", "QUOT.END"}:
            default = "201E" if node.tag == "QUOT.START" else "201C"
            parts.append(chr(int(node.get("CODE", default), 16)))
            return
        parts.append(node.text or "")
        for child in node:
            walk(child, quoted_article)
            parts.append(child.tail or "")
        if node.tag in {"P", "TXT", "ROW", "CELL"} or (quoted_article and node.tag in {
                "TI.ART", "STI.ART", "NO.P", "NO.PARAG", "ALINEA", "PARAG"}):
            parts.append(" ")

    walk(el)
    return _clean("".join(parts))


def _formex_list(el: ET.Element) -> list[tuple[str, str]]:
    items = []
    for item in el.findall("ITEM"):
        marker, body = "", []
        for child in item:
            if child.tag == "NP":
                marker = _clean("".join(child.find("NO.P").itertext())) if child.find("NO.P") is not None else ""
                txt = child.find("TXT")
                if txt is not None:
                    body.append(_formex_text(txt))
                for sub in child:
                    if sub.tag == "P":
                        body.append(_formex_text(sub))
                    elif sub.tag == "LIST":
                        body.extend(f"{m} {t}".strip() for m, t in _formex_list(sub))
            elif child.tag == "P":
                body.append(_formex_text(child))
            elif child.tag == "LIST":
                body.extend(f"{m} {t}".strip() for m, t in _formex_list(child))
            elif child.tag == "ALINEA":
                body.extend(t for kind, t in _formex_segments(child) if kind == "text")
        items.append((marker, _clean(" ".join(b for b in body if b))))
    return items


def _formex_segments(el: ET.Element) -> list[tuple[str, Any]]:
    """("text", str) per paragraph block and ("list", [(marker, text)]) in order."""
    segments: list[tuple[str, Any]] = []
    loose: list[str] = [el.text or ""]

    def flush() -> None:
        text = _clean("".join(loose))
        if text:
            segments.append(("text", text))
        loose.clear()

    for child in el:
        if child.tag in {"P", "ALINEA"}:
            flush()
            nested = [c for c in child if c.tag == "LIST"]
            if nested:
                segments.extend(_formex_segments(child))
            else:
                text = _formex_text(child)
                if text:
                    segments.append(("text", text))
        elif child.tag == "LIST":
            flush()
            segments.append(("list", _formex_list(child)))
        elif child.tag == "TBL":
            flush()
            segments.append(("text", _formex_text(child)))
        elif child.tag == "NOTE":
            pass
        else:
            loose.append(" " + _formex_text(child) + " " if child.tag not in {
                "QUOT.START", "QUOT.END", "HT", "FT", "DATE", "REF.DOC.OJ"} else _formex_text(child))
        loose.append(child.tail or "")
    flush()
    return segments


def render_blocks(paragraphs: list[tuple[str | None, list[tuple[str, Any]]]]) -> str:
    """Absätze (marker, segments) -> one Unterabsatz per line (module docstring)."""
    lines: list[str] = []
    for marker, segments in paragraphs:
        current = f"{marker} " if marker else ""
        opened = False
        for kind, value in segments:
            if kind == "text":
                if opened:
                    lines.append(current.rstrip())
                    current = ""
                current += value
                opened = True
            else:
                points = " ".join(f"{m} {t}".strip() for m, t in value)
                current = f"{current.rstrip()} {points}".strip() if current.strip() else \
                    f"{current}{points}"
                opened = True
        if current.strip():
            lines.append(current.rstrip())
    return "\n".join(_clean(line) for line in lines if line.strip())


def _marker(value: str) -> str | None:
    value = _clean(value)
    if not value:
        return None
    match = re.fullmatch(r"\(?(\d+[a-z]?)[.)]?", value)
    return f"({match.group(1)})" if match else value


def _formex_articles(node: ET.Element) -> Iterable[ET.Element]:
    """Yield each article once, keeping its quoted amendments and annexes out of the index."""
    if node.tag == "ARTICLE":
        yield node
    elif node.tag not in {"ANNEX", "QUOT.S"}:
        for child in node:
            yield from _formex_articles(child)


def parse_formex(data: bytes) -> dict[str, Any]:
    """Articles of one Formex 4 act (OJ ``ACT`` or consolidated ``CONS.ACT``)."""
    root = ET.fromstring(data)
    articles, annexes = [], 0
    for article in _formex_articles(root):
        label = _clean("".join(article.find("TI.ART").itertext())) if article.find("TI.ART") is not None else ""
        match = re.match(r"Artikel\s+(\d+\s*[a-z]?)", label)
        if not match:
            continue
        title_el = article.find("STI.ART")
        paragraphs: list[tuple[str | None, list[tuple[str, Any]]]] = []
        for child in article:
            if child.tag == "PARAG":
                no = child.find("NO.PARAG")
                segments: list[tuple[str, Any]] = []
                for part in child:
                    if part.tag == "ALINEA":
                        segments.extend(_formex_segments(part))
                    elif part.tag == "LIST":
                        segments.append(("list", _formex_list(part)))
                paragraphs.append((_marker("".join(no.itertext())) if no is not None else None,
                                   segments))
            elif child.tag in {"ALINEA", "P"}:
                paragraphs.append((None, _formex_segments(child) if child.tag == "ALINEA"
                                   else [("text", _formex_text(child))]))
            elif child.tag == "LIST":
                paragraphs.append((None, [("list", _formex_list(child))]))
        text = render_blocks(_merge_unnumbered(paragraphs))
        if not text:
            continue
        articles.append({"enbez": f"Art. {match.group(1).replace(' ', '')}",
                         "titel": _formex_text(title_el) if title_el is not None else "",
                         "text": text})
    annexes = sum(1 for _ in root.iter("ANNEX"))
    info = root.find("INFO.CONSLEG")
    corrigenda = []
    for corrig in root.iter("CORRIG"):
        celex = corrig.findtext(".//NO.CELEX")
        date = corrig.find(".//BIB.INSTANCE.CONS/DATE")
        ref = corrig.find(".//DOCUMENT.REF.CONS")
        corrigenda.append({
            "celex": celex, "date": _iso(date.get("ISO")) if date is not None else None,
            "oj_ref": (f"ABl. {ref.findtext('COLL')} {ref.findtext('NO.OJ')}/{ref.findtext('YEAR')}"
                       f", S. {ref.findtext('PAGE.FIRST')}") if ref is not None else None})
    return {"articles": articles, "annexes_skipped": annexes,
            "consleg": ({k.lower(): v for k, v in info.attrib.items()} if info is not None else None),
            "corrigenda": corrigenda}


def _merge_unnumbered(paragraphs):
    """Consecutive unnumbered blocks form the unnumbered body of the article."""
    merged: list[tuple[str | None, list[tuple[str, Any]]]] = []
    for marker, segments in paragraphs:
        if marker is None and merged and merged[-1][0] is None:
            merged[-1][1].extend(segments)
        else:
            merged.append((marker, list(segments)))
    return merged


def _iso(value: str | None) -> str | None:
    if value and re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return None


_HEADING = re.compile(r"^(?:KAPITEL|Kapitel|TITEL|Titel|ABSCHNITT|Abschnitt|TEIL|Teil)\s+[IVXLC\d]+[a-z]?$")
_END = re.compile(r"^(?:Geschehen zu|Im Namen des|Für das Europäische Parlament|Für den Rat)")
_POINT = re.compile(r"^((?:[a-z]{1,2}|[ivx]+|\d+[a-z]?)\)|\d+\.|[—–-])\s+(.*)$", re.S)


def html_blocks(data: bytes) -> list[dict[str, str]]:
    """Paragraph blocks of an OJ XHTML/HTML page in document order.

    A table row of two cells (marker, text) is one list point; nested tables are
    flattened into their row.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data.decode("utf-8", "replace"), "html.parser")
    body = soup.body or soup
    # footnote calls ("(1)" links) and footnote bodies are not norm text
    for anchor in body.find_all("a", href=re.compile(r"^#ntr")):
        anchor.decompose()
    for note in body.find_all("p", class_="note"):
        note.decompose()
    blocks: list[dict[str, str]] = []
    for node in body.find_all(["p", "tr"]):
        if node.name == "tr":
            if node.find_parent("tr") is not None:
                continue
            cells = node.find_all("td", recursive=False)
            if len(cells) >= 2:
                marker = _clean(cells[0].get_text(" "))
                text = _clean(" ".join(c.get_text(" ") for c in cells[1:]))
                if marker and text:
                    blocks.append({"kind": "point", "marker": marker, "text": text, "class": ""})
            continue
        if node.find_parent("tr") is not None:
            continue
        text = _clean(re.sub(r"\s*\[\d{1,3}\]", "", node.get_text(" ")))
        if text and not re.match(r"^\[\d{1,3}\]\s", _clean(node.get_text(" "))):
            blocks.append({"kind": "p", "text": text, "class": " ".join(node.get("class") or [])})
    return blocks


def parse_html_articles(data: bytes) -> dict[str, Any]:
    """Articles of an OJ HTML/XHTML document (legacy acts, per-article treaty works)."""
    blocks = html_blocks(data)
    articles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    skip_title = False
    for block in blocks:
        text, klass = block["text"], block.get("class", "")
        if block["kind"] == "p":
            label = _ARTICLE_LABEL.match(text)
            if label and ("ti-art" in klass or not klass or klass == "normal"):
                current = {"number": label.group(1), "title": "", "paragraphs": [], "expect_title": True}
                articles.append(current)
                skip_title = False
                continue
            if current is None:
                continue
            if _END.match(text) or text.startswith("ANHANG"):
                current = None
                continue
            if _HEADING.match(text):
                skip_title = True
                continue
            if skip_title:
                skip_title = False
                continue
            if re.fullmatch(r"[-_=—–.·\s]{3,}", text):
                current = None          # separator: footnotes follow
                continue
            if current["expect_title"] and ("sti-art" in klass or
                                            re.fullmatch(r"\(ex-Art(?:ikel|\.)[^()]*\)", text)):
                current["title"] = text
                continue
            current["expect_title"] = False
            point = _POINT.match(text) if not klass or klass == "normal" else None
            if point and current["paragraphs"]:
                current["paragraphs"][-1][1].append(("list", [(point.group(1), _clean(point.group(2)))]))
                continue
            marker = re.match(r"^\((\d+[a-z]?)\)\s+", text)
            if marker:
                current["paragraphs"].append((f"({marker.group(1)})", [("text", text[marker.end():])]))
            elif current["paragraphs"]:
                current["paragraphs"][-1][1].append(("text", text))
            else:
                current["paragraphs"].append((None, [("text", text)]))
        elif current is not None:
            current["expect_title"] = False
            point = ("list", [(block["marker"], block["text"])])
            if current["paragraphs"]:
                current["paragraphs"][-1][1].append(point)
            else:
                current["paragraphs"].append((None, [point]))
    out = []
    for article in articles:
        # adjacent list segments of one Absatz belong to one list
        paragraphs = []
        for marker, segments in article["paragraphs"]:
            joined: list[tuple[str, Any]] = []
            for kind, value in segments:
                if kind == "list" and joined and joined[-1][0] == "list":
                    joined[-1] = ("list", joined[-1][1] + value)
                else:
                    joined.append((kind, value))
            paragraphs.append((marker, joined))
        text = render_blocks(paragraphs)
        if text:
            out.append({"enbez": f"Art. {article['number']}", "titel": article["title"], "text": text})
    return {"articles": out, "annexes_skipped": 0, "consleg": None, "corrigenda": []}


def parse_edition(data: bytes, kind: str) -> dict[str, Any]:
    if kind == "fmx4" and data[:2] == b"PK":
        # act-by-act Official Journal (since 10/2023): the Formex item is a ZIP
        # of the act (``L_…DE.000101.fmx.xml``), its annexes, a TOC and a descriptor
        import io
        import zipfile
        merged = {"articles": [], "annexes_skipped": 0, "consleg": None, "corrigenda": []}
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for name in sorted(archive.namelist()):
                if not name.endswith(".xml") or ".toc." in name or ".doc." in name:
                    continue
                part = parse_formex(archive.read(name))
                merged["articles"].extend(part["articles"])
                merged["annexes_skipped"] += part["annexes_skipped"]
                merged["corrigenda"].extend(part["corrigenda"])
                merged["consleg"] = merged["consleg"] or part["consleg"]
        return merged
    if kind == "fmx4":
        return parse_formex(data)
    return parse_html_articles(data)


# ------------------------------------------------------------------ editions

PREFERENCE = ("fmx4", "xhtml", "html")


def fetch_edition(cellar: Cellar, celex: str, manifs: dict[str, str]) -> dict[str, Any]:
    kind = next((k for k in PREFERENCE if k in manifs), None)
    if kind is None:
        raise RuntimeError(f"{celex}: no German fmx4/xhtml/html manifestation")
    urls: list[str] = []
    if kind != "fmx4":
        # single-document HTML manifestations: the content item is DOC_1
        first = manifs[kind].replace("http://", "https://") + "/DOC_1"
        try:
            cellar.get(first)
            urls = [first]
        except Exception:  # noqa: BLE001 — fall back to the item listing
            urls = []
    urls = urls or manifestation_items(cellar, manifs[kind])
    if not urls:
        raise RuntimeError(f"{celex}: manifestation {manifs[kind]} lists no items")
    articles, items, parsed_meta = [], [], {"annexes_skipped": 0, "corrigenda": [], "consleg": None}
    for url in urls:
        got = cellar.get(url)
        items.append({k: got.get(k) for k in ("url", "sha256", "bytes_len", "retrieved_at",
                                              "content_type")})
        try:
            parsed = parse_edition(got["bytes"], kind)
        except ET.ParseError:
            continue
        articles.extend(parsed["articles"])
        parsed_meta["annexes_skipped"] += parsed["annexes_skipped"]
        parsed_meta["corrigenda"].extend(parsed["corrigenda"])
        parsed_meta["consleg"] = parsed_meta["consleg"] or parsed["consleg"]
    return {"celex": celex, "format": kind, "manifestation": manifs[kind], "items": items,
            "articles": _dedupe_articles(articles), **parsed_meta}


def _dedupe_articles(articles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for article in articles:
        if article["enbez"] in seen:
            continue
        seen.add(article["enbez"])
        out.append(article)
    return out


def fetch_article_works(cellar: Cellar, base: str, count: int) -> dict[str, Any]:
    """A treaty or the Charter: one Cellar work per article (``12016E078``)."""
    celexes = [f"{base}{n:03d}" for n in range(1, count + 1)]
    manifs = manifestations(cellar, celexes)
    articles, items, missing = [], [], []
    for celex in celexes:
        kinds = manifs.get(celex) or {}
        if not kinds:
            missing.append(celex)
            continue
        edition = fetch_edition(cellar, celex, kinds)
        items.extend(edition["items"])
        number = str(int(celex[len(base):]))
        found = [a for a in edition["articles"] if a["enbez"] == f"Art. {number}"]
        if not found:
            missing.append(celex)
            continue
        articles.append(found[0])
    return {"celex": base, "format": "xhtml_per_article", "manifestation": None, "items": items,
            "articles": articles, "annexes_skipped": 0, "corrigenda": [], "consleg": None,
            "missing_article_works": missing}


def act_id_of(celex: str) -> str:
    return "eu_" + re.sub(r"[^a-z0-9]", "", celex.casefold())


def fetch_act(cellar: Cellar, spec: dict[str, Any], *, lisbon: dict[str, Any] | None
              ) -> dict[str, Any]:
    celex = spec["celex"]
    meta = work_metadata(cellar, celex)
    warnings: list[str] = []
    editions: list[dict[str, Any]] = []
    rel = {"corrected_by": [], "repealed_by": [], "implicitly_repealed_by": [], "amended_by": []}
    if spec.get("family") in {"treaty", "charter"}:
        if spec.get("base"):
            base_meta = work_metadata(cellar, spec["base"])
            base = fetch_article_works(cellar, spec["base"], spec["articles"])
            editions.append({**base, "kind": "treaty_consolidated", "date": base_meta["published"],
                             "published": base_meta["published"], "oj_ref": base_meta["oj_ref"],
                             "valid_from": (lisbon or {}).get("in_force"),
                             "valid_from_basis": (f"Vertrag von Lissabon ({LISBON}) in Kraft "
                                                  f"{(lisbon or {}).get('in_force')} (CELLAR)"
                                                  if lisbon and lisbon.get("in_force") else None)})
        current = fetch_article_works(cellar, celex, spec["articles"])
        valid_from = meta["in_force"] if spec.get("valid_from_policy") == "entry_into_force" else None
        editions.append({**current, "kind": "treaty_consolidated", "date": meta["published"],
                         "published": meta["published"], "oj_ref": meta["oj_ref"],
                         "valid_from": valid_from,
                         "valid_from_basis": ("CELLAR entry into force of the Charter articles "
                                              f"({meta['in_force']})" if valid_from else None)})
        for edition in editions:
            if edition.get("missing_article_works"):
                warnings.append(f"{edition['celex']}: no German text for "
                                f"{len(edition['missing_article_works'])} article works")
    else:
        rel = related(cellar, celex)
        cons = consolidations(cellar, celex)
        manifs = manifestations(cellar, [celex] + [c["celex"] for c in cons])
        try:
            oj = fetch_edition(cellar, celex, manifs.get(celex) or {})
            editions.append({**oj, "kind": "oj", "date": meta["published"],
                             "published": meta["published"], "oj_ref": meta["oj_ref"]})
        except Exception as exc:  # noqa: BLE001 — consolidations may still carry the text
            warnings.append(f"{celex}: Official Journal text unavailable ({exc})")
        for row in cons:
            try:
                edition = fetch_edition(cellar, row["celex"], manifs.get(row["celex"]) or {})
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{row['celex']}: consolidated text unavailable ({exc})")
                continue
            editions.append({**edition, "kind": "consolidated", "date": row["date"]})
    editions = [e for e in editions if e.get("articles")]
    if not editions:
        raise RuntimeError(f"{celex}: no edition parsed into articles")
    dates = {k: meta[k] for k in ("adopted", "signed", "published", "oj_ref", "in_force",
                                  "applies_from", "entry_into_force_dates", "end_of_validity", "eli")}
    if spec.get("family") == "treaty" and lisbon:
        # The per-article works carry the original treaty's dates (1957/1958, 1992/1993);
        # the wording in force is the Lisbon consolidation.
        dates = {**{k: lisbon.get(k) for k in dates}, "adopted": lisbon.get("signed"),
                 "basis": f"Vertrag von Lissabon ({LISBON}, CELLAR metadata)",
                 "article_work_dates": {k: meta[k] for k in ("signed", "in_force")}}
    elif spec.get("family") == "charter":
        dates = {**dates, "adopted": meta.get("signed"),
                 "basis": "Charta in der am 12.12.2007 angepassten Fassung (CELLAR metadata of "
                          f"{celex}); the Official Journal date is the 2016 republication"}
    return {
        "act_id": act_id_of(celex), "celex": celex, "jurabk": spec["jurabk"],
        "aliases": spec.get("aliases") or [], "family": spec.get("family"),
        "title": meta["title"], "dates": dates,
        "relations": rel, "extends": spec.get("extends"),
        "editions": editions, "warnings": warnings,
        "source": {"name": "Publications Office of the EU (CELLAR)", "sparql": SPARQL,
                   "reuse": "Decision 2011/833/EU; EUR-Lex legal notice"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--acts", help="probe these comma-separated CELEX IDs in a separate snapshot")
    parser.add_argument("--offline", action="store_true", help="cache only (no network)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    sys.path.insert(0, str(ROOT / "pipeline"))
    from common import snapshot_dir, write_jsonl

    source, cache = "eu_texts", CACHE
    wanted = set()
    if args.acts is not None:
        parts = [part.strip() for part in args.acts.split(",")]
        if not all(parts):
            parser.error("--acts requires comma-separated CELEX identifiers")
        wanted = set(parts)
        selection = hashlib.sha256("\n".join(sorted(wanted)).encode()).hexdigest()
        source = f"eu_texts_probes/{selection}"
        cache = CACHE.parent / "eu_texts_probes" / selection
    cellar = Cellar(offline=args.offline, cache=cache)
    specs = [dict(s) for s in CORPUS]
    if not wanted or wanted - {s["celex"] for s in specs}:
        try:
            extensions = extension_decisions(cellar)
        except Exception as exc:  # noqa: BLE001
            print(f"[err] extension discovery failed, snapshot not written: {exc}", file=sys.stderr)
            return 1
        for celex in extensions:
            if all(s["celex"] != celex for s in specs):
                year, number = celex[1:5], int(celex[6:])
                specs.append({"celex": celex, "jurabk": f"DB (EU) {year}/{number}",
                              "family": "decision", "extends": EXTENDED_ACT,
                              "aliases": [f"Durchführungsbeschluss (EU) {year}/{number}"]})
    if wanted:
        missing = sorted(wanted - {s["celex"] for s in specs})
        if missing:
            print(f"[err] unknown EU corpus identifiers: {', '.join(missing)}", file=sys.stderr)
            return 1
        specs = [s for s in specs if s["celex"] in wanted]
    lisbon = None
    if any(s.get("base") for s in specs):
        lisbon = work_metadata(cellar, LISBON)
    rows, failed = [], []
    for spec in specs:
        try:
            row = fetch_act(cellar, spec, lisbon=lisbon)
        except Exception as exc:  # noqa: BLE001 — count and go on
            failed.append(spec["celex"])
            print(f"[err] {spec['celex']}: {exc}", file=sys.stderr)
            continue
        rows.append(row)
        print(f"  {spec['celex']:>12} {spec['jurabk']:<20} editions "
              f"{len(row['editions'])} articles {len(row['editions'][-1]['articles'])}"
              + (f" warnings {len(row['warnings'])}" if row["warnings"] else ""))
    if not rows or failed:
        print(f"[err] incomplete EU text corpus (failed={failed}); snapshot not written",
              file=sys.stderr)
        return 1
    out = snapshot_dir(source)
    write_jsonl(out / "acts.jsonl", rows)
    print(f"{len(rows)} acts, {cellar.requests} requests -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
