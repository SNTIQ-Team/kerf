"""Bavaria.

gesetze-bayern.de (Bayern.Recht) answers every path, including robots.txt,
with a Myra Security challenge (HTTP 503 "Security Check").  It is not
bypassed; ``python -m courts sync --source by`` probes it once and records the
typed ``challenge`` failure, then collects the official decision PDFs the
courts publish themselves:

* BayVGH and the six Bavarian VGs: decisions attached to press releases
  (vgh.bayern.de, no robots.txt);
* Bayerischer Verfassungsgerichtshof: selected and further decisions
  (bayern.verfassungsgerichtshof.de, robots allows all);
* LAG Nürnberg / LAG München decision database (arbg.bayern.de, robots allows all).

These are partial by nature; coverage says so.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from ..casenumbers import split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash
from .pdftext import layout_blocks, pdf_text

PORTAL_PROBE = "https://www.gesetze-bayern.de/Search/Filter/DOKTYP/rspr"
VGH = "https://www.vgh.bayern.de"
VGH_COURTS = {"bayvgh": "Bayerischer Verwaltungsgerichtshof", "vgmuenchen": "Verwaltungsgericht München",
              "vgaugsburg": "Verwaltungsgericht Augsburg", "vgregensburg": "Verwaltungsgericht Regensburg",
              "vgwuerzburg": "Verwaltungsgericht Würzburg", "vgbayreuth": "Verwaltungsgericht Bayreuth",
              "vgansbach": "Verwaltungsgericht Ansbach"}
VERFGH = "https://www.bayern.verfassungsgerichtshof.de"
VERFGH_LISTS = ("/bayverfgh/rechtsprechung/ausgewaehlte_entscheidungen.php",
                "/bayverfgh/rechtsprechung/weitere_entscheidungen.php")
ARBG = "https://www.arbg.bayern.de"
LAGS = {"nuernberg": "Landesarbeitsgericht Nürnberg", "muenchen": "Landesarbeitsgericht München"}
HEADINGS = {"leitsatz": "leitsatz", "leitsätze": "leitsatz", "entscheidung": "tenor", "beschluss": "tenor",
            "urteil": "tenor", "für recht erkannt": "tenor", "tenor": "tenor", "gründe": "gruende",
            "tatbestand": "tatbestand", "entscheidungsgründe": "entscheidungsgruende"}
MONTHS = ("januar februar märz april mai juni juli august september oktober november dezember").split()


def encode_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=quote(parts.path, safe="/%._-~")))


def parse_vgh_press(markup: str, court_key: str) -> list[dict]:
    items = []
    rows = re.findall(r'<tr><td>(\d\d\.\d\d\.\d{4})</td><td><a href="([^"]+)"[^>]*>(.*?)</a></td><td[^>]*>(.*?)</td></tr>',
                      markup, re.S)
    info = resolve_court(VGH_COURTS[court_key], default_land="by")
    for press_date, _pm, title, cell in rows:
        for href, az in re.findall(r'<a href="([^"]+\.pdf)"[^>]*>([^<]+)</a>', cell):
            if not re.search(r"\d", az):
                continue
            url = encode_url(urljoin(VGH, href))
            items.append({"source_doc_id": f"vgh:{urlsplit(url).path}", "url": url, "listed_date": None,
                          "listed_court": info.court_id, "listed_case_numbers": squash(az),
                          "listed_modified": None, "priority": priority(info.jurisdiction),
                          "extra": {"route": "vgh_press", "court_name": VGH_COURTS[court_key],
                                    "press_date": iso_date(press_date), "press_title": squash(re.sub("<[^>]+>", "", title))}})
    return items


def parse_verfgh_list(markup: str) -> list[dict]:
    items = []
    for href, title, when in re.findall(r'<li class="pdf">\s*<a href="([^"]+\.pdf)"[^>]*>(.*?)</a>\s*<span>([^<]+)</span>',
                                        markup, re.S):
        url = encode_url(urljoin(VERFGH, href))
        try:
            listed = iso_date(when)
        except UpstreamFailure:
            listed = None
        items.append({"source_doc_id": f"verfgh:{urlsplit(url).path}", "url": url, "listed_date": listed,
                      "listed_court": "verfgh-by", "listed_case_numbers": None, "listed_modified": None,
                      "priority": 1, "extra": {"route": "verfgh", "court_name": "Bayerischer Verfassungsgerichtshof",
                                               "title": squash(re.sub("<[^>]+>", "", title))}})
    return items


def parse_lag_list(markup: str, lag: str) -> list[dict]:
    items = []
    for az, when, href in re.findall(r'<h2>([^<]+)</h2>\s*<span><strong>(\d\d\.\d\d\.\d{4})</strong></span>.*?'
                                     r'<a href="([^"]+/entscheidungen/neue/\d+/[^"]*)"', markup, re.S):
        url = urljoin(ARBG, href.replace("index.php", ""))
        az, court_name, court_id = squash(az), LAGS[lag], f"lag-{lag}"
        # The LAG databases also list first-instance decisions: "ArbG München - 6 BV 41/19".
        first = re.match(r"ArbG\s*([A-ZÄÖÜ][\wäöüß]+)\s*-?\s*(.+)$", az)
        if first:
            court_name = f"Arbeitsgericht {first.group(1)}"
            court_id = resolve_court(court_name, default_land="by").court_id
            az = squash(first.group(2))
        items.append({"source_doc_id": f"lag:{urlsplit(url).path}", "url": url, "listed_date": iso_date(when),
                      "listed_court": court_id, "listed_case_numbers": az, "listed_modified": None,
                      "priority": 1, "extra": {"route": "lag", "court_name": court_name}})
    return items


def lag_form(lag: str, year: int) -> dict:
    form = {"ort": lag, "q": "", "month": "01", "year": str(year), "month_bis": "12", "year_bis": str(year),
            "Abschicken_ent": ""}
    for key in ("urteil", "urteil_bestand", "urteil_forderung", "urteil_sonstiges", "beschluss", "ta",
                "ta_streitwert", "ta_prozesskosten", "ta_zwang", "ta_kosten", "ta_sonstiges"):
        form[key] = "1"
    return form


def _german_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\.\s*(" + "|".join(MONTHS) + r")\s+(\d{4})", text, re.I)
    if m:
        return date(int(m.group(3)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(1))).isoformat()
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat() if m else None


DATE = r"\d{1,2}\.\s*(?:[A-Za-zäÄ]+\s+|\d{1,2}\.\s*)\d{4}"
DECISION_KINDS = r"Beschluss|Urteil|Gerichtsbescheid|Zwischenurteil|Endurteil|Teilurteil"


def _decision_type_and_date(head: str) -> tuple[str | None, str | None]:
    """Cover line "Beschluss des 10. Senats vom 16. Juni 2026" / "Beschluss der 2. Kammer vom 03.06.2025",
    else the rubrum's "... am 12. März 2025 folgendes Urteil"."""
    flat = " ".join(re.sub(r"(?m)^\s*-\s*\d{1,3}\s*-\s*$", " ", head).split())  # drop page markers
    for pattern in (rf"({DECISION_KINDS})\s+(?:des|der)\s+.{{0,60}}?vom\s+({DATE})",
                    rf"(?:am|vom)\s+({DATE})\s+folgende[ns]?\s*:?\s*({DECISION_KINDS})",
                    rf"({DECISION_KINDS})\s+vom\s+({DATE})",
                    rf"(Verkündet|Verkündung)\s+am:?\s+({DATE})"):
        m = re.search(pattern, flat)
        if m:
            kind, when = (m.group(1), m.group(2)) if not m.group(1)[0].isdigit() else (m.group(2), m.group(1))
            if kind.startswith("Verkünd"):
                kind = "Urteil"  # only judgments are pronounced ("verkündet")
            return kind, _german_date(when)
    return None, None


def sections_from_blocks(blocks: list[Block], basis: str) -> tuple[list[Block], list[Section]]:
    """Split at heading blocks; text before the first heading is the preamble."""
    preamble: list[Block] = []
    sections: list[Section] = []
    current: Section | None = None
    for block in blocks:
        if block.kind == "heading":
            name = HEADINGS[block.text.rstrip(":").lower()] if block.text.rstrip(":").lower() in HEADINGS else None
            name = name or HEADINGS.get(block.text.replace(" ", "").rstrip(":").lower())
            if name:
                if current is not None and current.name == name:
                    continue  # repeated heading ("Beschluss:" twice): same section continues
                if current and current.blocks:
                    sections.append(current)
                current = Section(name, "court", f"{basis}; heading '{block.text}'", heading=block.text)
                continue
        if current is None:
            preamble.append(block)
        else:
            current.blocks.append(block)
    if current and current.blocks:
        sections.append(current)
    return preamble, sections


def parse_pdf_decision(pdf: bytes, item: dict, url: str) -> ParsedDecision:
    extra = item["extra"] if isinstance(item["extra"], dict) else __import__("json").loads(item["extra"])
    route = extra["route"]
    text = pdf_text(pdf, url=url)
    listed_az = item["listed_case_numbers"]
    headers = {listed_az} if listed_az else set()
    head = text[:6000]
    metadata: dict = {"route": route, "preamble_omitted": True}
    procedural = []
    court_name = extra["court_name"]
    decision_type = None
    raw_az = listed_az
    when = item["listed_date"]
    leitsatz_cover: list[str] = []
    if route == "vgh_press":
        head = "\f".join(text.split("\f")[:4])
        rule = re.search(r"^\s*[-_]{10,}\s*$", head, re.M)
        cover = head[:rule.start()] if rule and "Aktenzeichen:" in head[:rule.start()] else ""
        m = re.search(r"Aktenzeichen:\s*(.+)", cover)
        raw_az = squash(m.group(1)) if m else listed_az
        headers.add(raw_az)
        decision_type, when = _decision_type_and_date(head)
        if not when:
            raise UpstreamFailure("parse_error", "BayVGH/VG PDF: no decision date found", url=url)
        norms = re.search(r"Rechtsquellen:\s*\n(.*?)\n\s*\n", cover, re.S)
        if norms:
            metadata["norms"] = [n.strip() for n in re.split(r";\s*\n?", " ".join(norms.group(1).split())) if n.strip()]
        points = re.search(r"Hauptpunkte:\s*\n(.*?)\n\s*\n", cover, re.S)
        if points:
            metadata["hauptpunkte"] = " ".join(points.group(1).split())
        ls = re.search(r"Leitsätze?:\s*\n(.*)$", cover, re.S)
        if ls and ls.group(1).strip():
            leitsatz_cover = [" ".join(p.split()) for p in re.split(r"\n\s*\n", ls.group(1)) if p.strip()]
        v = re.search(r"\(([^,()]+),\s*(?:Entscheidung|Urteil|Beschluss|Gerichtsbescheid)\s+vom\s+"
                      r"(\d{1,2}\.\s*\w+\s+\d{4}),\s*Az\.?:\s*([^)]+)\)", head)
        if v:
            procedural.append({"direction": "previous", "court": v.group(1).strip(), "date": _german_date(v.group(2)),
                               "case_number": squash(v.group(3)), "decision_type": None, "raw": squash(v.group(0)),
                               "field": "cover:Vorinstanz", "origin": "source_metadata"})
        m = re.search(r"Gericht:\s*(\S+)", cover)
        metadata["cover_court"] = m.group(1) if m else None
    elif route == "verfgh":
        m = re.search(r"Aktenzeichen:\s*(.+)", head)
        if m:
            raw_az = squash(m.group(1))
        else:
            # Joined proceedings: "I. Vf. 5-VIII-18 ... II. Vf. 7-VII-18" in the header, no label.
            header = re.split(r"\n\s*(?:Leitsätze?|Entscheidung|Gründe)\s*:", head)[0]
            dockets = list(dict.fromkeys(re.findall(r"Vf\.\s*\d+[a-z]?-[IVX]+-\d{2}", header)))
            if not dockets:
                raise UpstreamFailure("parse_error", "BayVerfGH PDF without Aktenzeichen", url=url)
            raw_az = ", ".join(" ".join(d.split()) for d in dockets)
            metadata["case_numbers_raw_constructed"] = "joined 'Vf.' dockets from the PDF header"
        headers.add(raw_az)
        m = re.search(r"(Entscheidung|Beschluss|Urteil)\s+des\s+Bayerischen\s+Verfassungsgerichtshofs?\s+vom\s+"
                      r"(\d{1,2}\.\s*\w+\s+\d{4})", head)
        if not m:
            raise UpstreamFailure("parse_error", "BayVerfGH PDF without decision date", url=url)
        decision_type, when = m.group(1), _german_date(m.group(2))
        if item["listed_date"] and item["listed_date"] != when:
            raise UpstreamFailure("identity_mismatch", f"listed {item['listed_date']} != PDF {when}", url=url)
    elif route == "lag":
        first = text.split("\f", 1)[0]
        if listed_az and re.sub(r"\s", "", listed_az) not in re.sub(r"\s", "", first):
            raise UpstreamFailure("identity_mismatch", f"LAG PDF first page lacks {listed_az!r}", url=url)
        decision_type = "Urteil" if re.search(r"\bURTEIL\b|\bUrteil\b", first) else (
            "Beschluss" if re.search(r"BESCHLUSS|Beschluss", first) else None)
        m = re.search(r"^\s*(\d+\s+\w+\s+\d+/\d{2})\s*\n\s*\((Arbeitsgericht [^)]+)\)", first, re.M)
        if m:
            procedural.append({"direction": "previous", "court": m.group(2), "date": None,
                               "case_number": m.group(1), "decision_type": None, "raw": squash(m.group(0)),
                               "field": "pdf:Vorinstanz", "origin": "source_metadata"})
    if not when or not raw_az:
        raise UpstreamFailure("parse_error", "missing decision date or Aktenzeichen", url=url)
    blocks = layout_blocks(text, running_headers=headers, headings=HEADINGS)
    preamble, sections = sections_from_blocks(blocks, f"court PDF ({route})")
    if leitsatz_cover and not any(s.name == "leitsatz" for s in sections):
        sections.insert(0, Section("leitsatz", "court", "cover sheet 'Leitsätze' of the court's own publication",
                                   [Block(text=t, locator=f"cover/leitsatz[{i}]") for i, t in enumerate(leitsatz_cover, 1)]))
    if route == "vgh_press":
        # The cover sheet's own "Leitsätze:" heading opens a section that runs into the rubrum; keep only
        # the cover text before the dashed rule (handled above) and drop the heading-derived duplicate.
        sections = [s for s in sections if not (s.name == "leitsatz" and s.heading and s.blocks
                                                and s.blocks[0].locator.startswith("page[1]"))] or sections
    if not any(s.name in ("gruende", "entscheidungsgruende", "tatbestand") for s in sections):
        tenor = [s for s in sections if s.name == "tenor"]
        if not tenor:
            raise UpstreamFailure("parse_error", "no reasoning section recognised in PDF", url=url)
        # Tenor and reasons follow each other without a "Gründe" heading: not separable
        # without guessing, so the text is kept as one court-authored section.
        merged = [b for s in sections if s.name == "tenor" or sections.index(s) > sections.index(tenor[0])
                  for b in s.blocks]
        sections = [s for s in sections if sections.index(s) < sections.index(tenor[0])] + [
            Section("sonstiger_langtext", "court",
                    "court PDF without a 'Gründe'/'Tatbestand' heading: tenor and reasons not separable",
                    merged, heading=tenor[0].heading)]
        metadata["sections_not_separable"] = True
    info = resolve_court(court_name, default_land="by")
    return ParsedDecision(
        source_id="by", source_doc_id=item["source_doc_id"], source_url=url, court_name=court_name,
        decision_date=when, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=decision_type, sections=sections, ecli=None, court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction, land="by", title=extra.get("title") or extra.get("press_title"),
        metadata=metadata, procedural=procedural)


class BayernAdapter(Adapter):
    source_id = "by"
    label = ("Bayern: gesetze-bayern.de (blocked by bot challenge) + official court publications "
             "(BayVGH/VG press decisions, BayVerfGH, LAG Nürnberg/München)")
    base_url = "https://www.gesetze-bayern.de"
    land = "by"
    adapter_name = "by-court-pdfs"

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        items: list[dict] = []
        portal = None
        try:
            http.get(PORTAL_PROBE, timeout=30)
            portal = "reachable"
        except UpstreamFailure as failure:
            portal = f"{failure.kind}: {failure.detail}"
            conn.execute("INSERT INTO failures(run_id, source_id, source_doc_id, url, kind, detail, http_status, at) "
                         "VALUES(NULL, 'by', NULL, ?, ?, ?, ?, datetime('now'))",
                         (failure.url, failure.kind, "gesetze-bayern.de portal: " + failure.detail[:300], failure.status))
        conn.execute("UPDATE sources SET status_reason=? WHERE source_id='by'",
                     (f"gesetze-bayern.de: {portal}; partial coverage via official court PDFs",))
        failures: list[UpstreamFailure] = []

        def page(fetch, parse) -> None:
            # One unavailable list page is recorded; the other routes still run.
            try:
                response = fetch()
                items.extend(parse(decode_html(response.body, response.headers)))
            except UpstreamFailure as failure:
                failures.append(failure)
                conn.execute("INSERT INTO failures(run_id, source_id, source_doc_id, url, kind, detail, "
                             "http_status, at) VALUES(NULL, 'by', NULL, ?, ?, ?, ?, datetime('now'))",
                             (failure.url, failure.kind, "listing: " + failure.detail[:300], failure.status))

        for key in VGH_COURTS:
            page(lambda key=key: http.get(f"{VGH}/gerichte/{key}/presse/pressemitteilungen/index.php", timeout=60),
                 lambda markup, key=key: parse_vgh_press(markup, key))
        for path in VERFGH_LISTS:
            page(lambda path=path: http.get(VERFGH + path, timeout=60), parse_verfgh_list)
        this_year = date.today().year
        first_year = max(2020, int(since[:4])) if since else 2020
        for lag in LAGS:
            for year in range(this_year, first_year - 1, -1):
                url = f"{ARBG}/die-arbeitsgerichtsbarkeit-in-bayern/lag-{lag}/entscheidungen/"
                page(lambda url=url, lag=lag, year=year: http.post(url, data=lag_form(lag, year), timeout=60),
                     lambda markup, lag=lag: parse_lag_list(markup, lag))
        if not items and failures:
            raise failures[0]
        if since:
            items = [i for i in items if not i["listed_date"] or i["listed_date"] >= since]
        seen, unique = set(), []
        for item in items:
            if item["source_doc_id"] not in seen:
                seen.add(item["source_doc_id"])
                unique.append(item)
        return Listing(items=unique, upstream_total=None, complete=False)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        extra = item["extra"] if isinstance(item["extra"], dict) else __import__("json").loads(item["extra"])
        url = item["url"]
        if extra["route"] == "lag":
            detail = http.get(url, timeout=60)
            markup = decode_html(detail.body, detail.headers)
            m = re.search(r'<a[^>]+href="([^"]+\.pdf)"[^>]*title="Volltext-PDF"', markup) or \
                re.search(r'<a[^>]+title="Volltext-PDF"[^>]*href="([^"]+\.pdf)"', markup)
            if not m:
                raise UpstreamFailure("unexpected_representation", "LAG detail page without full-text PDF", url=url)
            url = encode_url(urljoin(ARBG, m.group(1)))
        response = http.get(url, timeout=120)
        if not response.body.startswith(b"%PDF"):
            raise UpstreamFailure("unexpected_representation",
                                  f"expected PDF, got {response.content_type or 'unknown'}", url=url)
        return Capture(body=response.body, url=url, media_type="application/pdf", extension="pdf",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        return parse_pdf_decision(capture.body, dict(item), capture.url)


def adapters() -> dict[str, Adapter]:
    return {"by": BayernAdapter()}
