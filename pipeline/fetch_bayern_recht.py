"""Fetch consolidated Bavarian law HEAD + amendment back-history from
gesetze-bayern.de (BAYERN.RECHT, Bayerische Staatskanzlei).

VERIFIED endpoints (2026-07-06, robots fully permissive "Allow: /"):
  /Content/Document/ffn   one ~1.1 MB HTML page: the complete BayRS
      systematic register. Act rows are <td>BayRS-Nr</td><td><a
      href="KEY">title</a></td>; the rows after each act carry the
      Fortfuehrungsnachweis amendment chain, numbered like
      "1) Art. 1 neu gefasst ... (§ 2 G v. 10.09.2007, S. 634)".
  /Content/Zip/<KEY>      application/zip with bayportalnorm/<KEY>.xml
      (DOCTYPE byrecht-norm, C.H.Beck DTD): builddate attribute,
      inkraft/ausserkraft, einzelnorm per Art./§ (para.nr, para.titel,
      jurAbsatz), aenderungsverlauf, typed verweis.* cross-references.

Official abbreviations and register document keys differ (AufnG ->
"BayAsylAufnG"). Resolve by exact FFN document key and compare the
BayRS number and XML identity before accepting the complete source archive.
EU references in the corpus XML are encoded as verweis.norm with
ersatz dokids "EU_RL_*", "EWG_VO_*", "EWG_DSGVO", "EUGRCharta*"; the
DTD's verweis.eurl/verweis.euvo elements are also handled when present.
They occur in norm bodies (enbez = "Art. N"/"§ N") and in fn.def
implementation footnotes on aenderungsverlauf entries (enbez =
"aenderungsverlauf:<seq>", joinable against versions.jsonl).

Output (data/snapshots/bayern_recht/<date>/):
    acts.jsonl     {key, jurabk, long_title, bayrs_nr, builddate,
                    inkraft, norm_count}
    norms.jsonl    {key, jurabk, enbez, titel, text}
    versions.jsonl {jurabk, seq, date, gvbl_citation, description,
                    source: "ffn" | "xml"}
    eu_refs.jsonl  {jurabk, enbez, ref_type, target}

Usage:
    python3 pipeline/fetch_bayern_recht.py
    python3 pipeline/fetch_bayern_recht.py --abbrs GO,BayBO --output-dir /tmp/by-sample

The dated snapshot includes the original FFN HTML and all accepted ZIPs under
raw/. A partial selection must use its own output directory. To replay an
existing capture, give --raw-input, --observed-on and --output-dir.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import tempfile
from datetime import date
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup

if __package__:
    from .common import ROOT, SNAPSHOTS, Http, write_jsonl
    from .reader_source_cache import reader_act_id, save_reader_source
else:
    from common import ROOT, SNAPSHOTS, Http, write_jsonl
    from reader_source_cache import reader_act_id, save_reader_source

BASE = "https://www.gesetze-bayern.de"
FFN_URL = BASE + "/Content/Document/ffn"
ZIP_URL = BASE + "/Content/Zip/{key}"

# Official abbreviation (public identifier) -> document key linked from the
# official FFN. These can differ substantially, e.g. AGAufenthG -> BayZGAusland.
CORPUS = {
    "AGAufenthG": "BayZGAusland",
    "AGBBiG": "BayAGBBiG",
    "AGBGB": "BayAGBGB",
    "AGFGO": "BayAGFGO",
    "AGG 10": "BayAGG_10",
    "AGGVG": "BayAGGVG",
    "AGPStG": "BayAGPStG",
    "AGSG": "BayAGSG",
    "AGSGG": "BayAGSGG",
    "AGVwGO": "BayAGVwGO",
    "AGZweigstV": "BayAGZweigstV",
    "AVBayFwG": "BayAVFwG",
    "AVBayHIG": "BayAVHIG",
    "AVBayKiBiG": "BayAVKiBiG",
    "AVBayRDG": "BayAVRDG",
    "AVBaySchFG": "BayAVSchFG",
    "AVBaySozKiPädG": "BayAVSozKiPaedG",
    "AVPStG": "BayVVPStG",
    "AVPfleWoqG": "BayAVPfleWoqG",
    "AVSG": "BayAVSG",
    "AbmG": "BayAbmG",
    "ArbGOrgG": "BayOrgGAG",
    "ArbSchV": "BayArbSchV",
    "AufbewV": "BayAufbewV",
    "AufnG": "BayAsylAufnG",
    "BBiGHwOV": "BayBBiGHwOV",
    "BFSO": "BayBFSO2023",
    "BFSO Gesundheit": "BayBFSOGesundheit",
    "BSO": "BayBSO",
    "BSO-F": "BayBSOF",
    "BauVorlV": "BayBauVorlV2008",
    "BayAGBAföG": "BayAGBAfoeG",
    "BayAGBtG": "BayAGBtG",
    "BayAbfG": "BayAbfAlG",
    "BayAbgG": "BayAbgG",
    "BayAföG": "BayAfoeG",
    "BayArbGGTV": "BayAbhGertArbV",
    "BayArbZustG": "BayArbZustG",
    "BayAzV": "BayAzV",
    "BayBG": "BayBG",
    "BayBGG": "BayBGG",
    "BayBO": "BayBO",
    "BayBQFG": "BayBQFG",
    "BayBeamtVG": "BayBeamtVG",
    "BayBeauftrG": "BayBeauftrG",
    "BayBesG": "BayBesG",
    "BayBhV": "BayBhV",
    "BayBlindG": "BayBlindG",
    "BayBodSchG": "BayBodSchG",
    "BayDG": "BayDG",
    "BayDSG": "BayDSG",
    "BayDSchG": "BayDSchG",
    "BayDarlRueVerwG": "BayDarlRueVerwG",
    "BayDiG": "BayDiG",
    "BayDiV": "BayDiV",
    "BayDokZugV": "BayDokZugV",
    "BayEG": "BayEG",
    "BayESG": "BayEBG",
    "BayEUG": "BayEUG",
    "BayFAG": "BayFAG",
    "BayFGV": "BayFGV",
    "BayFamGG": "BayFamGG",
    "BayFiG": "BayFischG",
    "BayFraktG": "BayFraktG",
    "BayFwG": "BayFwG",
    "BayGMPP": "BayAGBMG",
    "BayGVFG": "BayGVFG",
    "BayGaV": "BayGutAV",
    "BayGlG": "BayGlG",
    "BayGrStG": "BayGrStG",
    "BayHIG": "BayHIG",
    "BayHO": "BayHO",
    "BayHZG": "BayHZG",
    "BayHintG": "BayHintG",
    "BayHärteV": "BayHaerteV",
    "BayImSchG": "BayImSchG",
    "BayIntG": "BayIntG",
    "BayJAVollzG": "BayJAVollzG",
    "BayKHV": "BayKHV",
    "BayKRegG": "BayKRegG",
    "BayKSG": "BayKatSchutzG",
    "BayKiBiG": "BayKiBiG",
    "BayKlimaG": "BayKlimaG",
    "BayKommV": "BayBekV",
    "BayKrG": "BayKrG",
    "BayLBG": "BayLBG",
    "BayLPflGG": "BayLPflGG",
    "BayLTGeschO": "BayLTGO",
    "BayLTUntG": "BayLTUntG",
    "BayLadSchlG": "BayLadSchlG",
    "BayLobbyRG": "BayLobbyRG",
    "BayLplG": "BayLplG",
    "BayMG": "BayMG",
    "BayMRVG": "BayMRVG",
    "BayMinG": "BayStaatsRRVG",
    "BayNatSchG": "BayNatSchG",
    "BayPVG": "BayPVG",
    "BayPetG": "BayPetG",
    "BayPfleG": "BayPfleVG",
    "BayPrG": "BayPrG",
    "BayPsychKHG": "BayPsychKHG",
    "BayRDG": "BayRDG",
    "BayRKG": "BayRKG",
    "BayRSG": "BayRSG",
    "BayRadG": "BayRadG",
    "BayRettSanV": "BayRettSanV",
    "BayRiStAG": "BayRiStAG",
    "BaySchFG": "BaySchFG",
    "BaySchO": "BaySchO2016",
    "BaySchlG": "BaySchlG",
    "BaySchwBerG": "BaySchwBerG",
    "BaySchwBerV": "BaySchBerV",
    "BaySenG": "BaySenG_2023",
    "BaySozBAG": "BaySozKiPaedG",
    "BayStOpferhG": "BayStOpferhG",
    "BayStVollzG": "BayStVollzG",
    "BayStatG": "BayStatG",
    "BayStrAG": "BayStrAG",
    "BayStrWG": "BayStrWG",
    "BaySvVollzG": "BaySvVollzG",
    "BaySÜG": "BaySichPruefG",
    "BayUIG": "BayUIG",
    "BayUPZV": "BayUPZV",
    "BayUVollzG": "BayUVollzG",
    "BayUniKlinG": "BayUniKlinG",
    "BayVSG": "BayVSG",
    "BayVerf": "BayVerf",
    "BayVersG": "BayVersG08",
    "BayVwVfG": "BayVwVfG",
    "BayWG": "BayWG",
    "BayWaldG": "BayWaldG",
    "BayWoBindG": "BayWoBindG",
    "BayWoFG": "BayWoFG",
    "BayeAktV-V": "BayeAktVV",
    "BayÖPNVG": "BayOePNVG",
    "BestG": "BayBestG",
    "BezO": "BayBezO",
    "BezWG": "BayBezWG",
    "DBauV": "BayDBauV",
    "DVAsyl": "BayDVAsyl",
    "DVBayAföG": "BayDVAfoeG",
    "DVPOG": "BayDVPOG",
    "DVVwZVG": "BayDVVwZVG",
    "DVWoR": "BayDVWoR",
    "EBV": "BayEBV",
    "ERVV Ju": "BayERVV",
    "FOBOSO": "BayFOBOSO",
    "FSO": "BayFSO",
    "FTG": "BayFTG",
    "FVGebO": "BayFVGebO",
    "FakO": "BayFakO",
    "FeuV": "BayFeuV2008",
    "GDG": "BayGDG",
    "GDPO": "BayGDPO",
    "GDozPO": "BayGKPO",
    "GLKrWG": "BayGLKrWG",
    "GLKrWO": "BayGLKrWO",
    "GO": "BayGO",
    "GSG": "BayGSG",
    "GSO": "BayGSO",
    "GStVO-ArbG": "BayGStVOArbG",
    "GZVJu": "BayGZVJu",
    "GaStellV": "BayGaV",
    "GebOVerm": "BayGebOVerm",
    "GerOrgG": "BayGerOrgG",
    "GerZahlV": "BayGerZahlV",
    "GesV": "BayGesV",
    "GeschStV": "BayGeschStVO",
    "GrKrV": "BayGrKrV",
    "GrSO": "BayVSO",
    "HFKomV": "BayHFKomV",
    "HG 2026/2027": "BayHG2026_2027",
    "HKaG": "BayHKaG",
    "HUnterrV": "BayHausuV",
    "HZV": "BayHZV",
    "JBeitrGVBV": "BayJBeitrOVBV",
    "JSOG": "BayJSOG",
    "KAG": "BayKAG",
    "KG": "BayKG",
    "KSO": "BayKraSO",
    "KUV": "BayKUV",
    "KVz": "BayKVzKG",
    "KWBG": "BayKWBG",
    "KommHV-Doppik": "BayKommHVDoppik",
    "KommHV-Kameralistik": "BayKommHV",
    "KommPrV": "BayKommPrV",
    "KommStaGebG": "BayKomGlG",
    "KommZG": "BayKommZG",
    "LABV": "BayLABV",
    "LBRV": "BayLBRV",
    "LJKostG": "BayJVKostG",
    "LKrO": "BayLKrO",
    "LStVG": "BayLStVG",
    "LWG": "BayLWG",
    "LlbG": "BayLlbG",
    "MSO": "BayMSO",
    "MeldDV": "BayMeldDV",
    "PAG": "BayPAG",
    "PKGG": "BayPKGG",
    "POG": "BayPOG",
    "PfleWoqG": "BayPfleWoqG",
    "PolKV": "BayPolKV",
    "PrüfVBau": "BayPruefVBau",
    "QualV": "BayQualV",
    "RHG": "BayRHG",
    "RSO": "BayRSO",
    "SWG": "BaySWG",
    "SchBefV": "BaySchBefV",
    "SchKfrG": "BaySchulKostG",
    "SchulbauV": "BaySchulBauV",
    "StKO": "BayStKO2019",
    "StudWV": "BayStudWV",
    "UmlegAusschV": "BayUmlegungsV",
    "UrlMV": "BayUrlMV",
    "VGemO": "BayVGemO",
    "VSO-F": "BayVSOF",
    "VStättV": "BayVStaettV",
    "VfGHG": "BayVfGHG",
    "VwZVG": "BayVwZVG",
    "WSO": "BayWSO",
    "ZuSEVO": "BayZuSEVO",
    "ZustG": "BayZustG",
    "ZustGVerk": "BayZustGVerk",
    "ZustV": "BayZustV",
    "ZustV-GA": "BayZustVGA",
    "ZustVAuslR": "BayZustVAuslR",
    "ZustVBau": "BayBauGBZustV",
    "ZwEWG": "BayZwEWG2008",
    "eAktV ArbSozG": "BayeAktVArbSozG",
}

DATE_RE = re.compile(r"v(?:om)?\.?\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})")
EU_DOKID_RE = re.compile(r"^(?:EU_|EWG_|EG_|EUGR)")


def flat(el: ET.Element) -> str:
    """Flatten an XML subtree to whitespace-normalized plain text."""
    txt = ET.tostring(el, encoding="unicode", method="text")
    return " ".join(txt.split())


def iso_date(text: str, first: bool = False) -> str:
    """D.M.YYYY date in text (after 'v.'/'vom') as ISO, or ''.
    ffn citation strings want the LAST date (default); XML
    aenderungsverlauf bodies want the FIRST — nested parentheticals like
    '(GVBl. S. 282, geänd. durch G v. 23.6.2016 …)' cite amendments OF
    the amending act, and taking the last date shifted 5 rows by up to
    12 years (confirmed against the independent ffn chain)."""
    hits = list(DATE_RE.finditer(text))
    if not hits:
        return ""
    d, m, y = hits[0].groups() if first else hits[-1].groups()
    return f"{y}-{int(m):02d}-{int(d):02d}"


# --------------------------------------------------------------- ffn


def parse_ffn(html: str) -> list[dict]:
    """Register in document order: doc_key, bayrs_nr, title, chain."""
    soup = BeautifulSoup(html, "html.parser")
    register: list[dict] = []
    current: dict | None = None
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) != 2:
            continue
        a = tds[1].find("a")
        href = (a.get("href") or "") if a else ""
        if a and href and "/" not in href:
            current = {"doc_key": href,
                       "bayrs_nr": tds[0].get_text(strip=True),
                       "title": " ".join(a.get_text().split()),
                       "chain": []}
            register.append(current)
            continue
        if current is None or tds[0].get_text(strip=True):
            continue                        # section header / stray row
        txt = " ".join(tds[1].get_text().split())
        m = re.match(r"^(\d+)\)\s*(.*)$", txt)
        if m:
            current["chain"].append(
                parse_chain_entry(int(m.group(1)), m.group(2)))
    return register


def parse_chain_entry(seq: int, rest: str) -> dict:
    """'Art. 1 neu gefasst (§ 2 G v. 10.09.2007, S. 634)' -> fields."""
    cit, desc = "", rest.strip()
    m = re.search(r"\(([^()]*)\)\s*$", desc)
    if m:
        cit = m.group(1).strip()
        desc = desc[:m.start()].strip()
    return {"seq": seq, "description": desc, "gvbl_citation": cit,
            "date": iso_date(cit)}


# --------------------------------------------------------------- xml


def eu_ref_type(dokid: str) -> str:
    if "_RL_" in dokid:
        return "eurl"
    if "_VO_" in dokid or "DSGVO" in dokid:
        return "euvo"
    return "eu"


def eu_refs_of(el: ET.Element) -> list[tuple[str, str]]:
    """(ref_type, target) pairs for EU references under el."""
    refs: list[tuple[str, str]] = []
    for node in el.iter():
        if node.tag == "verweis.eurl":
            nr = (node.get("v.rl-eu-nr")
                  or f"{node.get('v.rl-jahr', '')}/{node.get('v.rl-nr', '')}"
                  .strip("/")) or flat(node)
            refs.append(("eurl", nr))
        elif node.tag == "verweis.euvo":
            nr = (node.get("v.vo-eu-nr")
                  or f"{node.get('v.vo-jahr', '')}/{node.get('v.vo-nr', '')}"
                  .strip("/")) or flat(node)
            refs.append(("euvo", nr))
        elif node.tag == "verweis.norm":
            for sub in node.iter():
                dokid = sub.get("ersatz") or ""
                if EU_DOKID_RE.match(dokid):
                    refs.append((eu_ref_type(dokid), dokid))
    return refs


def own_norms(body: ET.Element):
    """Yield statutory units of the act, excluding quoted amending content."""
    for child in body:
        if child.tag == "einzelnorm":
            yield child
        elif child.tag == "gliederung":
            yield from own_norms(child)


def parse_act_xml(xml_bytes: bytes, key: str, abbr: str, *,
                  include_annexes: bool = False,
                  ) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """-> (act, norms, xml_versions, eu_refs) for one bayportalnorm doc."""
    root = ET.fromstring(xml_bytes)
    jurabk = (root.findtext("kopf/angaben.versabh/amtlicheAbk")
              or "").strip() or abbr
    bayrs = (root.findtext(".//gliederungsNr.BayRS") or "").strip()
    bayrs = re.sub(r"^BayRS\s*", "", bayrs)
    act = {"key": key, "jurabk": jurabk, "long_title": "",
           "bayrs_nr": bayrs, "builddate": root.get("builddate", ""),
           "inkraft": (root.findtext(".//inkraft") or "").strip(),
           "norm_count": 0}

    norms, refs = [], []
    seen_refs: set[tuple[str, str]] = set()
    body = root.find("rumpf") if include_annexes else root
    if body is None:
        raise ValueError(f"{key}: no statute body")
    elements = own_norms(body) if include_annexes else body.iter("einzelnorm")
    for en in elements:
        enbez = " ".join((en.findtext("para.nr") or "").split())
        tit = en.find("para.titel")
        parts = [flat(c) for c in en
                 if c.tag not in ("para.nr", "para.titel")]
        norms.append({"key": key, "jurabk": jurabk, "enbez": enbez,
                      "titel": flat(tit) if tit is not None else "",
                      "text": "\n".join(p for p in parts if p)})
        for ref_type, target in eu_refs_of(en):
            if (enbez, target) not in seen_refs:
                seen_refs.add((enbez, target))
                refs.append({"jurabk": jurabk, "enbez": enbez,
                             "ref_type": ref_type, "target": target})
    if include_annexes:
        for annex in root.findall("annex/annex.koerper"):
            label = (annex.findtext("annex.nummer[@int]") or annex.findtext("annex.nummer") or "").strip()
            title_el = annex.find("annex.titel")
            heading = flat(title_el) if title_el is not None else ""
            if not label:
                # GLKrWO has an unnumbered introductory Anlagenverzeichnis.
                if not heading:
                    raise ValueError(f"{key}: annex without a label or title")
                label, heading = heading, ""
            long_label = (annex.findtext("annex.nummer") or "").strip()
            parts = [long_label] if long_label and long_label != label else []
            for part in annex:
                if part.tag in {"annex.nummer", "annex.titel"}:
                    continue
                # Keep text in tables, paragraph breaks and descriptions of
                # embedded media. Original binary attachments remain in the ZIP.
                part = copy.deepcopy(part)
                for el in part.iter():
                    if el.tag == "graphic" and el.get("Desc"):
                        el.text = el.get("Desc")
                    if el.tag in {"p", "tr", "einzelnorm", "jurAbsatz"}:
                        el.tail = "\n" + (el.tail or "")
                    elif el.tag in {"td", "th"}:
                        el.tail = " " + (el.tail or "")
                lines = (" ".join(line.split()) for line in "".join(part.itertext()).splitlines())
                parts.extend(line for line in lines if line)
            norms.append({"key": key, "jurabk": jurabk, "enbez": label, "norm_kind": "annex",
                          "titel": heading, "text": "\n".join(parts)})
            for ref_type, target in eu_refs_of(annex):
                if (label, target) not in seen_refs:
                    seen_refs.add((label, target))
                    refs.append({"jurabk": jurabk, "enbez": label, "ref_type": ref_type,
                                 "target": target})
    act["norm_count"] = len(norms)

    versions = []
    av = root.find(".//aenderungsverlauf")
    for li in (av.findall(".//li") if av is not None else []):
        sym = (li.findtext("symbol") or "").strip()
        seq = int(sym.rstrip(".")) if sym.rstrip(".").isdigit() else 0
        # implementation footnotes carry the EU refs of this amendment
        enbez = f"aenderungsverlauf:{seq}"
        for ref_type, target in eu_refs_of(li):
            if (enbez, target) not in seen_refs:
                seen_refs.add((enbez, target))
                refs.append({"jurabk": jurabk, "enbez": enbez,
                             "ref_type": ref_type, "target": target})
        clean = copy.deepcopy(li)           # drop footnote text from body
        for node in clean.iter():
            for child in list(node):
                if child.tag != "fn.call":
                    continue
                tail = child.tail or ""     # keep tail (holds "vom <date>")
                kids = list(node)
                i = kids.index(child)
                if i > 0:
                    kids[i - 1].tail = (kids[i - 1].tail or "") + tail
                else:
                    node.text = (node.text or "") + tail
                node.remove(child)
        body = flat(clean)
        if sym and body.startswith(sym):
            body = body[len(sym):].strip()
        m = re.search(r"\((GVBl\.[^(),]*)", body)
        cit = m.group(1).strip() if m else ""
        date = iso_date(body, first=True)
        desc = body.split("(GVBl", 1)[0].strip()
        versions.append({"jurabk": jurabk, "seq": seq, "date": date,
                         "gvbl_citation": cit, "description": desc,
                         "source": "xml"})
    return act, norms, versions, refs


def parse_registered_act(zip_bytes: bytes, entry: dict, abbreviation: str):
    key = entry["doc_key"]
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        member = archive.getinfo(f"bayportalnorm/{key}.xml")
        if member.file_size > 40 * 1024**2:
            raise ValueError(f"{key}: oversized XML")
        xml = archive.read(member)
    root = ET.fromstring(xml)
    identity = root.find("kopf/angaben.versunabh/dokumentation")
    if root.tag != "byrecht-norm" or identity is None or identity.get("dokid") != key:
        raise ValueError(f"{key}: source document identity mismatch")
    act, norms, xml_versions, refs = parse_act_xml(xml, key, abbreviation, include_annexes=True)
    if act["jurabk"] != abbreviation or act["bayrs_nr"] != entry["bayrs_nr"]:
        raise ValueError(f"{key}: XML abbreviation or BayRS number differs from the register mapping")
    if not any(norm["enbez"] and norm["text"] for norm in norms):
        raise ValueError(f"{key}: empty norm text")
    act["long_title"] = re.sub(r"\s*(?:in der Fassung der Bekanntmachung\s+)?"
                               r"vom\s+\d{1,2}\.\s.*$", "", entry["title"])
    act.update(includes_xml_annexes=True, source_url=ZIP_URL.format(key=key),
               source_sha256=hashlib.sha256(zip_bytes).hexdigest(),
               xml_sha256=hashlib.sha256(xml).hexdigest())
    versions = [{"jurabk": abbreviation, **row, "source": "ffn"} for row in entry["chain"]]
    return act, norms, versions + xml_versions, refs, xml


def write_snapshot(stage: Path, abbreviations, get_bytes, observed_on: str) -> None:
    raw = stage / "raw"
    raw.mkdir()
    ffn = get_bytes("ffn.html", FFN_URL)
    entries = parse_ffn(ffn.decode("utf-8-sig"))
    (raw / "ffn.html").write_bytes(ffn)
    register = {entry["doc_key"]: entry for entry in entries}
    if not entries or len(register) != len(entries):
        raise ValueError("empty or ambiguous official FFN document register")
    ffn_sha = hashlib.sha256(ffn).hexdigest()
    tables = {name: [] for name in ("acts", "norms", "versions", "eu_refs")}
    for abbr in abbreviations:
        key = CORPUS[abbr]
        if key not in register:
            raise ValueError(f"{key}: not listed in the official FFN")
        blob = get_bytes(f"{key}.zip", ZIP_URL.format(key=key))
        act, norms, versions, refs, xml = parse_registered_act(blob, register[key], abbr)
        act.update(source_observed_on=observed_on, ffn_sha256=ffn_sha)
        tables["acts"].append(act)
        tables["norms"].extend(norms)
        tables["versions"].extend(versions)
        tables["eu_refs"].extend(refs)
        (raw / f"{key}.zip").write_bytes(blob)
        save_reader_source(stage / "reader-source", reader_act_id("by", abbr), xml)
        print(f"  {abbr}: {len(norms)} norms, {len(versions)} amendment records", flush=True)
    for name, rows in tables.items():
        write_jsonl(stage / f"{name}.jsonl", rows)


def verify_same_observation(existing: Path, candidate: Path) -> None:
    """A rerun may receive a new request token in otherwise identical FFN HTML."""
    for name in ("norms.jsonl", "versions.jsonl", "eu_refs.jsonl"):
        if (existing / name).read_bytes() != (candidate / name).read_bytes():
            raise ValueError(f"conflicting existing snapshot: {existing / name}")

    def evidence(directory):
        ffn = (directory / "raw" / "ffn.html").read_bytes()
        sha = hashlib.sha256(ffn).hexdigest()
        acts = []
        for line in (directory / "acts.jsonl").read_text(encoding="utf-8").splitlines():
            act = json.loads(line)
            if act.pop("ffn_sha256") != sha:
                raise ValueError(f"damaged FFN evidence: {directory}")
            acts.append(act)
        return acts, parse_ffn(ffn.decode("utf-8-sig"))

    if evidence(existing) != evidence(candidate):
        raise ValueError(f"conflicting existing snapshot: {existing}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--abbrs", help="comma-separated official abbreviations")
    ap.add_argument("--output-dir", type=Path, help="standalone snapshot destination")
    ap.add_argument("--raw-input", type=Path, help="replay captured ffn.html and original ZIPs")
    ap.add_argument("--observed-on", type=date.fromisoformat, help="original capture date for replay")
    args = ap.parse_args()
    if args.abbrs and not args.output_dir:
        ap.error("--abbrs requires a separate --output-dir")
    if args.raw_input and not (args.observed_on and args.output_dir):
        ap.error("--raw-input requires --observed-on and --output-dir")
    if args.observed_on and not args.raw_input:
        ap.error("--observed-on requires --raw-input")
    aliases = {key: abbr for abbr, key in CORPUS.items()}
    wanted = {aliases.get(s.strip(), s.strip()) for s in args.abbrs.split(",")} if args.abbrs else set(CORPUS)
    if wanted - set(CORPUS):
        ap.error(f"unknown abbreviations: {sorted(wanted - set(CORPUS))}")
    observed_on = (args.observed_on or date.today()).isoformat()
    destination = args.output_dir or SNAPSHOTS / "bayern_recht" / observed_on
    http = Http(delay=0.85)
    http.s.headers["User-Agent"] = "SNTIQ-kerf/0.1 (research; +https://sntiq.com/)"

    def get_bytes(filename, url):
        if args.raw_input:
            return (args.raw_input / filename).read_bytes()
        response = http.get(url, timeout=90, retries=1)
        response.raise_for_status()
        return response.content

    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bayern-", dir=destination.parent) as temporary:
        stage = Path(temporary) / "snapshot"
        stage.mkdir()
        write_snapshot(stage, sorted(wanted), get_bytes, observed_on)
        if destination.exists():
            verify_same_observation(destination, stage)
        else:
            stage.rename(destination)
        if not args.output_dir:
            for xml_path in (destination / "reader-source").glob("by_*.xml"):
                save_reader_source(ROOT / "data" / "reader-source", xml_path.stem, xml_path.read_bytes())
    print(f"{len(wanted)} Bavarian acts -> {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
