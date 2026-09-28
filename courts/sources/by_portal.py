"""BAYERN.RECHT (www.gesetze-bayern.de) decision pages captured in the operator's own browser.

The portal answers automated requests with a Myra Security challenge.  It is never bypassed and never
crawled: the operator opens ONE decision page in Chrome (solving the challenge there if it appears), and
the saved page - HTML, or the portal's PDF of it - is handed to the corpus with
``python -m courts ingest-capture`` (courts.capture).  This module only parses such a capture.

Decision URLs: ``https://www.gesetze-bayern.de/Content/Document/<doc id>`` (HTML) and
``/Content/Pdf/<doc id>?all=False`` (PDF), doc id ``Y-300-Z-BECKRS-B-<year>-N-<number>`` = the BeckRS
citation "BeckRS <year>, <number>".  (``/Search/Suggest?…&suggester=azSuggest`` returns only a JSON list of
Aktenzeichen strings, no document ids; the search itself is a POST form with a session hit list.)

Page structure (Wayback captures 2023-2026; fixtures conformance/fixtures/decisions/bayern-recht-*):

* ``#doc-metadata`` (and ``<title>``): "<court>, <type> v. <dd.mm.yyyy> – <Az>" - "VGH München, Urteil v.
  18.01.2023 – 16b D 22.2183";
* ``.rsprbox``: labelled fields (``div.rsprboxueber``) "Titel:" (``h1.titelzeile``), "Normenkette(n):",
  "Leitsatz/Leitsätze:" (``div.leitsatz``; "(redaktioneller Leitsatz)" marks the portal editors' own
  headnote), "Schlagwort(e):", "Vorinstanz(en):", "Rechtsmittelinstanz(en):", "Fundstelle(n):"
  (``div.rsprboxzeile``);
* body: ``h2.entsueber`` headings (Tenor, Tatbestand, Entscheidungsgründe, Gründe) and ``div.rdblock`` =
  ``div.rd`` (the Randnummer, empty for unnumbered blocks) + ``div.absatz``; enumerations as
  ``dl > dt`` (marker) ``+ dd > div.rd + div.absatz``.

Randnummern are the portal's as published (``metadata.numbering_source = "bayern_recht"``: the portal's
documentation may number decisions the court left unnumbered).  Editorial parts are editorial sections
(``titel``; redaktionelle Leitsätze as ``orientierungssatz``) or metadata (Normenkette, Schlagworte,
Fundstellen); the court's text has no rubrum on the portal (``preamble_omitted``).
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from ..casenumbers import split_case_numbers
from ..courtids import fold, resolve as resolve_court
from ..errors import UpstreamFailure
from ..model import Block, ParsedDecision, Section
from .base import iso_date, squash
from .htmlblocks import checked_ecli, inline_text, is_outline_marker, table_text
from .pdftext import join_lines, pdf_text

PORTAL = "https://www.gesetze-bayern.de"
PORTAL_HOSTS = {"www.gesetze-bayern.de", "gesetze-bayern.de"}
DOC_ID = r"Y-300-Z-[A-Z0-9]+-B-\d{4}-N-\d{1,7}"
DOC_ID_RE = re.compile(DOC_ID)
URL_PATH_RE = re.compile(r"^/Content/(?P<kind>Document|Pdf)/(?P<doc>" + DOC_ID + r")/?$")
ROUTE = "portal_capture"

COURT_TYPES = {"VG": "Verwaltungsgericht", "SG": "Sozialgericht", "LArbG": "Landesarbeitsgericht",
               "LAG": "Landesarbeitsgericht", "ArbG": "Arbeitsgericht", "OLG": "Oberlandesgericht",
               "LG": "Landgericht", "AG": "Amtsgericht", "FG": "Finanzgericht"}
COURTS = {"VGH München": "Bayerischer Verwaltungsgerichtshof", "BayVGH": "Bayerischer Verwaltungsgerichtshof",
          "LSG München": "Bayerisches Landessozialgericht", "BayLSG": "Bayerisches Landessozialgericht",
          "VerfGH München": "Bayerischer Verfassungsgerichtshof", "BayVerfGH": "Bayerischer Verfassungsgerichtshof",
          "BayObLG": "Bayerisches Oberstes Landesgericht", "ObLG München": "Bayerisches Oberstes Landesgericht",
          "BFH München": "Bundesfinanzhof", "BFH": "Bundesfinanzhof", "BGH": "Bundesgerichtshof",
          "BVerwG": "Bundesverwaltungsgericht", "BSG": "Bundessozialgericht", "BAG": "Bundesarbeitsgericht",
          "BVerfG": "Bundesverfassungsgericht", "BPatG München": "Bundespatentgericht",
          "BPatG": "Bundespatentgericht"}
HEADINGS = {"tenor": "tenor", "tatbestand": "tatbestand", "entscheidungsgruende": "entscheidungsgruende",
            "gruende": "gruende"}
BOX_FIELDS = {"titel": "titel", "normenkette": "norms", "normenketten": "norms", "leitsatz": "leitsatz",
              "leitsaetze": "leitsatz", "schlagwort": "keywords", "schlagworte": "keywords",
              "vorinstanz": "previous", "vorinstanzen": "previous", "rechtsmittelinstanz": "subsequent",
              "rechtsmittelinstanzen": "subsequent", "fundstelle": "published_in", "fundstellen": "published_in",
              "ecli": "ecli"}
HEAD_RE = re.compile(r"^(?P<court>.+?),\s+(?P<type>\S[^,]*?)\s+v(?:om|\.)\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})"
                     r"\s+[–-]\s+(?P<az>\S.*)$")
PROC_RE = re.compile(r"^(?P<court>.+?)(?:,\s*(?P<type>[^,]+?))?\s+vom\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4}|-+)"
                     r"\s+[–-]\s+(?P<az>\S.*)$")
REDAKTIONELL = re.compile(r"\(\s*redaktione?ller\s+Leitsatz\s*\)", re.I)
TITLE_SUFFIX = re.compile(r"^\s*Bürgerservice\s+-\s+|\s+-\s+Bürgerservice\s*$")
NORM_UNIT = re.compile(r"(?:§§?|Artt?\.|Artikel)\s")

BASIS_TITLE = "BAYERN.RECHT 'Titel:' (documentation title written by the portal's editors)"
BASIS_COURT_LEITSATZ = ("BAYERN.RECHT 'Leitsätze:' entry without the portal's '(redaktioneller Leitsatz)' mark: "
                        "the court's own Leitsatz")
BASIS_EDITORIAL_LEITSATZ = "BAYERN.RECHT '(redaktioneller Leitsatz)': headnote written by the portal's editors"


class NotADecisionPage(UpstreamFailure):
    pass


def canonical_url(url: str) -> tuple[str, str]:
    """(doc id, canonical HTML URL) of a BAYERN.RECHT decision URL; ValueError for anything else
    (search pages, statutes, other hosts)."""
    parts = urlsplit((url or "").strip())
    if parts.scheme not in ("http", "https") or parts.netloc.lower() not in PORTAL_HOSTS:
        raise ValueError(f"not a gesetze-bayern.de URL: {url!r}")
    m = URL_PATH_RE.match(parts.path)
    if not m:
        raise ValueError(f"not a single BAYERN.RECHT decision URL (/Content/Document/<{DOC_ID}>): {url!r}")
    return m.group("doc"), f"{PORTAL}/Content/Document/{m.group('doc')}"


def court_long_name(short: str) -> str:
    short = squash(short)
    if short in COURTS:
        return COURTS[short]
    head, _, rest = short.partition(" ")
    if head in COURT_TYPES and rest:
        return f"{COURT_TYPES[head]} {rest}"
    return short


def _head(text: str, url: str) -> dict:
    m = HEAD_RE.match(squash(TITLE_SUFFIX.sub("", text or "")))
    if not m:
        raise NotADecisionPage("unexpected_representation",
                               f"no '<court>, <type> v. <date> – <Az>' line: {squash(text)[:120]!r}", url=url)
    return {"court_short": m.group("court"), "type": squash(m.group("type")), "date": iso_date(m.group("date")),
            "az": squash(m.group("az"))}


def _court(short: str, url: str):
    name = court_long_name(short)
    info = resolve_court(name, default_land="by")
    if info.court_type == "unknown":
        raise UpstreamFailure("parse_error", f"BAYERN.RECHT court {short!r} not recognised", url=url)
    return name, info


def restate_norm_chain(line: str) -> list[str]:
    """'BBG § 61 Abs. 1 S. 3, § 77 Abs. 1 S. 2' -> ['§ 61 Abs. 1 S. 3 BBG', '§ 77 Abs. 1 S. 2 BBG'] (the
    portal names the act first; the citation reader expects it after the unit)."""
    line = squash(line)
    m = NORM_UNIT.search(line)
    if not m or m.start() == 0:
        return [line] if line else []
    act, rest = line[:m.start()].strip(), line[m.start():]
    parts = re.split(r",\s*(?=(?:§§?|Artt?\.|Artikel)\s)", rest)
    return [f"{part.strip().rstrip(',;')} {act}" for part in parts if part.strip()]


def _procedural(line: str, direction: str, field: str) -> dict:
    raw = squash(line)
    m = PROC_RE.match(raw)
    if not m:
        return {"direction": direction, "court": None, "date": None, "case_number": None, "decision_type": None,
                "raw": raw, "field": field, "origin": "editorial_metadata"}
    when = m.group("date")
    return {"direction": direction, "court": court_long_name(m.group("court")),
            "date": None if when.startswith("-") else iso_date(when), "case_number": squash(m.group("az")),
            "decision_type": squash(m.group("type")) or None, "raw": raw, "field": field,
            "origin": "editorial_metadata"}


def _box_metadata(fields: dict[str, list[str]], metadata: dict, procedural: list[dict]) -> None:
    for key, values in fields.items():
        target = BOX_FIELDS.get(key)
        if target == "norms":
            metadata["norm_chains_raw"] = values
            metadata["norms"] = [n for v in values for n in restate_norm_chain(v)]
            metadata["norms_basis"] = "BAYERN.RECHT Normenkette, restated as '<unit> <act>'"
        elif target == "keywords":
            metadata["keywords"] = [k.strip() for v in values for k in v.split(",") if k.strip()]
        elif target == "published_in":
            metadata["published_in"] = values
            beck = [v for v in values if v.startswith("BeckRS ")]
            if beck:
                metadata["beckrs"] = beck[0]
        elif target in ("previous", "subsequent"):
            label = "Vorinstanz" if target == "previous" else "Rechtsmittelinstanz"
            procedural.extend(_procedural(v, target, f"BAYERN.RECHT:{label}") for v in values)
        elif target in ("titel", "leitsatz", "ecli"):
            continue
        else:
            metadata.setdefault("portal_fields", {})[key] = values


def _decision(item: dict, url: str, head: dict, sections: list[Section], metadata: dict, procedural: list[dict],
              ecli_raw: str | None) -> ParsedDecision:
    name, info = _court(head["court_short"], url)
    metadata.update({"portal": "gesetze-bayern.de", "numbering_source": "bayern_recht", "preamble_omitted": True,
                     "court_as_published": head["court_short"]})
    labels = [b.label for s in sections for b in s.blocks if b.label]
    gaps = [f"{a}->{b}" for a, b in zip(labels, labels[1:]) if not (a.isdigit() and b.isdigit() and int(b) == int(a) + 1)]
    if gaps:
        metadata["rn_sequence_irregular"] = gaps[:20]
    ecli = checked_ecli(ecli_raw, head["date"], metadata) if ecli_raw else None
    if not any(s.name in ("tenor", "tatbestand", "entscheidungsgruende", "gruende", "sonstiger_langtext")
               and s.blocks for s in sections):
        raise NotADecisionPage("unexpected_representation", "BAYERN.RECHT page without decision text", url=url)
    return ParsedDecision(
        source_id="by", source_doc_id=item["source_doc_id"], source_url=url, court_name=name,
        decision_date=head["date"], case_numbers_raw=head["az"], case_numbers=split_case_numbers(head["az"]),
        decision_type=head["type"], sections=sections, ecli=ecli, court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction, land=info.land or "by",
        title=next((s.blocks[0].text for s in sections if s.name == "titel" and s.blocks), None),
        metadata=metadata, procedural=procedural)


def _headnote_sections(title: str | None, leitsaetze: list[tuple[str, str]]) -> list[Section]:
    sections: list[Section] = []
    if title:
        sections.append(Section("titel", "editorial", BASIS_TITLE, [Block(title, locator="h1.titelzeile")]))
    court = [Block(t, locator=loc) for t, loc in leitsaetze if not REDAKTIONELL.search(t)]
    editorial = [Block(t, locator=loc) for t, loc in leitsaetze if REDAKTIONELL.search(t)]
    if court:
        sections.append(Section("leitsatz", "court", BASIS_COURT_LEITSATZ, court))
    if editorial:
        sections.append(Section("orientierungssatz", "editorial", BASIS_EDITORIAL_LEITSATZ, editorial))
    return sections


def _section_for(heading: str) -> str:
    return HEADINGS.get(fold(heading).rstrip(":").strip(), "sonstiger_langtext")


# --- HTML --------------------------------------------------------------------------------------------

def _absatz_text(el: Tag) -> str:
    if el.find("table"):
        parts = []
        for child in el.children:
            if isinstance(child, Tag) and child.name == "table":
                parts.append(table_text(child))
            else:
                text = inline_text([child])
                if text:
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return inline_text(list(el.children))


def parse_portal_html(body: bytes, item: dict, url: str) -> ParsedDecision:
    markup = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(markup, "html.parser")
    content = soup.find(id="content")
    if content is None or not (content.find(class_="rdblock") or content.find(class_="absatz")):
        raise NotADecisionPage("unexpected_representation",
                               "not a BAYERN.RECHT decision page (no #content with Randnummer blocks)", url=url)
    meta = soup.find(id="doc-metadata")
    head_text = inline_text([meta]).replace("\n", " ") if meta else (soup.title.get_text(" ") if soup.title else "")
    head = _head(head_text, url)
    box = content.find(class_="rsprbox")
    fields: dict[str, list[str]] = {}
    title = None
    leitsaetze: list[tuple[str, str]] = []
    if box is not None:
        label = None
        for index, el in enumerate(c for c in box.children if isinstance(c, Tag)):
            classes = el.get("class") or []
            if "rsprboxueber" in classes:
                label = fold(inline_text([el])).rstrip(":").strip()
                continue
            text = inline_text([el]).replace("\n", " ").strip()
            if not text:
                continue
            if "titelzeile" in classes:
                title = text
            elif "leitsatz" in classes:
                leitsaetze.append((inline_text([el]), f".rsprbox/div.leitsatz[{len(leitsaetze) + 1}]"))
            elif label:
                fields.setdefault(label, []).append(text)
    sections = _headnote_sections(title, leitsaetze)
    metadata: dict = {}
    procedural: list[dict] = []
    _box_metadata(fields, metadata, procedural)
    cont = content.find(class_="cont") or content
    current: Section | None = None
    images: list[dict] = []
    last_label = None
    position = 0

    def is_heading(t: Tag) -> bool:
        return t.name in ("h2", "h3") and "entsueber" in (t.get("class") or [])

    def is_absatz(t: Tag) -> bool:
        return t.name == "div" and "absatz" in (t.get("class") or [])

    for el in cont.find_all(lambda t: is_heading(t) or is_absatz(t)):
        if box is not None and el.find_parent(class_="rsprbox") is not None:
            continue
        if is_heading(el):
            heading = squash(el.get_text(" "))
            current = Section(_section_for(heading), "court", f"BAYERN.RECHT h2.entsueber '{heading}'",
                              heading=heading)
            sections.append(current)
            continue
        position += 1
        prev = el.find_previous_sibling(True)
        label = squash(prev.get_text(" ")) if prev is not None and "rd" in (prev.get("class") or []) else ""
        text = _absatz_text(el)
        if el.parent is not None and el.parent.name == "dd" and el.parent.find(class_="absatz") is el:
            dt = el.parent.find_previous_sibling(True)
            marker = squash(dt.get_text(" ")) if dt is not None and dt.name == "dt" else ""
            if marker:
                text = f"{marker} {text}" if text else marker
        if not text.strip():
            for img in el.find_all("img"):
                images.append({"src": img.get("src"), "rn": label or None, "after_rn": last_label,
                               "locator": f"absatz[{position}]"})
            continue
        if current is None:
            current = Section("sonstiger_langtext", "court", "BAYERN.RECHT text before the first heading")
            sections.append(current)
        kind = "heading" if not label and is_outline_marker(text) else "text"
        current.blocks.append(Block(text=text, label=label or None, kind=kind, locator=f"absatz[{position}]"))
        if label:
            last_label = label
    if images:
        metadata["images_omitted"] = images
    sections = [s for s in sections if s.blocks]
    return _decision(item, url, head, sections, metadata, procedural, (fields.get("ecli") or [None])[0])


# --- PDF (the portal's own PDF of the page) ------------------------------------------------------------

PDF_HEADINGS = {"tenor", "tatbestand", "entscheidungsgründe", "gründe"}
PDF_LABEL_RE = re.compile(r"^(?P<label>[A-ZÄÖÜ][A-Za-zäöüß]+(?:\s[A-Za-zäöüß]+)?):\s*$")
RN_ONLY = re.compile(r"^\s*(\d{1,4})\s*$")
PDF_LEITSATZ_START = re.compile(r"^\d{1,2}\.\s+\S")


def _pdf_leitsaetze(lines: list[str]) -> list[str]:
    """The box's Leitsätze: one per numbered item ("1. …"), each ending the line before the next number."""
    items: list[list[str]] = []
    for line in lines:
        if PDF_LEITSATZ_START.match(line.strip()) and (not items or re.search(r"[.)]\s*$", items[-1][-1])):
            items.append([line])
        elif items:
            items[-1].append(line)
        else:
            items.append([line])
    return [join_lines(i) for i in items if join_lines(i)]


def parse_portal_pdf(body: bytes, item: dict, url: str) -> ParsedDecision:
    text = pdf_text(body, url=url)
    # A page break never ends a block: the portal's PDF continues the sentence on the next page.
    lines = [line.rstrip() for line in text.replace("\f", "").split("\n")]
    filled = [i for i, line in enumerate(lines) if line.strip()]
    if not filled:
        raise NotADecisionPage("empty_document", "empty PDF", url=url)
    head = _head(lines[filled[0]], url)
    body_start = next((i for i in filled[1:] if lines[i].strip().lower() in PDF_HEADINGS), None)
    if body_start is None:
        raise NotADecisionPage("unexpected_representation", "BAYERN.RECHT PDF without decision headings", url=url)
    fields: dict[str, list[str]] = {}
    label = None
    grouped: dict[str, list[str]] = {}
    for i in range(filled[0] + 1, body_start):
        line = lines[i].strip()
        m = PDF_LABEL_RE.match(line)
        if m:
            label = fold(m.group("label"))
            grouped.setdefault(label, [])
            continue
        if label is not None:
            grouped[label].append(line)
    title = None
    leitsaetze: list[tuple[str, str]] = []
    for key, raw in grouped.items():
        target = BOX_FIELDS.get(key)
        if target == "titel":
            title = join_lines(raw)
        elif target == "leitsatz":
            leitsaetze = [(t, f"pdf/box/leitsatz[{n}]") for n, t in enumerate(_pdf_leitsaetze(raw), 1)]
        elif target == "keywords":
            fields[key] = [join_lines(raw)]
        else:
            fields[key] = [" ".join(r.split()) for r in raw if r.strip()]
    sections = _headnote_sections(title, leitsaetze)
    metadata: dict = {"representation": "bayern_recht_pdf"}
    procedural: list[dict] = []
    _box_metadata(fields, metadata, procedural)
    current: Section | None = None
    chunk: list[str] = []
    chunk_label: str | None = None
    chunk_line = 0
    expected = 1

    def flush() -> None:
        nonlocal chunk, chunk_label
        if not chunk:
            return          # a blank line right after a Randnummer: the Rn's text follows
        joined = join_lines(chunk)
        if joined and current is not None:
            kind = "heading" if chunk_label is None and is_outline_marker(joined) else "text"
            current.blocks.append(Block(text=joined, label=chunk_label, kind=kind, locator=f"pdf/line[{chunk_line}]"))
        chunk, chunk_label = [], None

    for i in range(body_start, len(lines)):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.lower() in PDF_HEADINGS:
            flush()
            current = Section(_section_for(stripped), "court", f"BAYERN.RECHT PDF heading '{stripped}'",
                              heading=stripped)
            sections.append(current)
            continue
        m = RN_ONLY.match(line)
        if m and int(m.group(1)) == expected:
            flush()
            chunk_label, chunk_line = m.group(1), i + 1
            expected += 1
            continue
        if not chunk:
            chunk_line = i + 1
        chunk.append(line)
    flush()
    sections = [s for s in sections if s.blocks]
    return _decision(item, url, head, sections, metadata, procedural, (fields.get("ecli") or [None])[0])


def parse_capture(body: bytes, item: dict, url: str) -> ParsedDecision:
    if body.startswith(b"%PDF"):
        return parse_portal_pdf(body, item, url)
    return parse_portal_html(body, item, url)


def page_doc_ids(body: bytes) -> set[str]:
    """Document ids the page itself names (its PDF link, 'getLink' anchors); empty for PDFs."""
    if body.startswith(b"%PDF"):
        return set()
    markup = body.decode("utf-8", errors="replace")
    return set(re.findall(r"/Content/(?:Pdf|Document)/(" + DOC_ID + r")", markup))
