#!/usr/bin/env python3
"""Replay the German Basic Law review offline from the retained German editions and gazettes.

Place the PDFs and DRV HTML files named in data/gg_edition_review.json in a directory and run
python tools/audit_gg_editions.py --sources DIR. Poppler supplies pdftotext.
The publisher files, their links and their SHA-256 values are in the review.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import gzip
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "data/gg_edition_review.json"
FIXTURE = ROOT / "tests/fixtures/verified_editions/gg-20260506.json.gz"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clean(text):
    text = re.sub(r"\u00ad\s*\n\s*", "", text)
    return " ".join(text.replace("\u00ad", "").split())


def one(pattern, text):
    matches = list(re.finditer(pattern, text, re.DOTALL))
    require(len(matches) == 1, f"expected one printed instruction {pattern[:55]}")
    return matches[0][1]


def replace_one(text, old, new):
    require(text.count(old) == 1, f"ambiguous or missing original wording {old[:60]}")
    return text.replace(old, new, 1)


def federal_edition(text):
    text = text.replace("\f", "\n")
    text = re.sub(r"(?m)^[ \t]*- Seite \d+ von \d+ -[ \t]*$", "", text)
    text = re.sub(r"(?im)^[ \t]*Ein Service des Bundesministeriums[^\n]+\n"
                  r"[ \t]*[^\n]*www\.gesetze-im-internet\.de[ \t]*$", "", text)
    text = re.sub(r"(?im)^[ \t]*Ein Service des Bundesministeriums[^\n]+\n"
                  r"[ \t]*[^\n]*www\.juris\.de[ \t]*$", "", text)
    opening = list(re.finditer(r"(?m)^Eingangsformel\s*$", text))
    require(len(opening) == 1, "missing opening formula")
    text = text[opening[0].start():]
    headings = list(re.finditer(r"(?m)^Art[ \t]+(\d+[a-z]*)(?:[ \t]+([^\n]*))?$", text))

    def layout(value):
        # The line break falls after a permanent hyphen in these words/citations.
        for left, right in (("Nordrhein-", "Westfalen"), ("Mecklenburg-", "Vorpommern"),
                            ("Schleswig-", "Holstein"), ("Sachsen-", "Anhalt"), ("100-", "2")):
            value = re.sub(re.escape(left) + r"[ \t]*\n[ \t]*" + re.escape(right), left + right, value)
        return clean(value)

    blocks = {"Eingangsformel und Präambel": layout(text[:headings[0].start()])}
    for index, heading in enumerate(headings):
        label, extra = heading[1], heading[2] or ""
        if label == "74a":
            require(extra.strip() == "und 75 (weggefallen)", "unexpected joint heading")
            label = "74a und 75"
        require(label not in blocks, f"duplicate article {label}")
        end = headings[index+1].start() if index+1 < len(headings) else len(text)
        body = text[heading.end():end]
        if label == "146":
            body, separator, annex = body.partition("\nAnhang EV\n")
            require(separator and annex.strip(), "missing Unification Treaty annex")
        blocks[label] = layout(extra + "\n" + body)
        if label == "146":
            blocks["Anhang EV"] = layout(annex)
    return blocks


def edition_digest(blocks):
    value = json.dumps(list(blocks.items()), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def enact_2025(old, gazette):
    """Apply all six amendment instructions printed in Article 1."""
    result = old.copy()
    new = clean(one(r"Artikel 109 Absatz 3 Satz 5 wird durch die folgenden Sätze ersetzt:\s*„(.*?)“", gazette))
    paragraph = one(r"\(3\) (.*?) \(4\)", old["109"])
    sentences = re.split(r"(?<=\.) ", paragraph)
    require(len(sentences) == 5, "unexpected sentence count in Art. 109(3)")
    result["109"] = replace_one(old["109"], sentences[4], new)

    instructions = clean(one(r"2\. Artikel 115 Absatz 2 wird wie folgt geändert:(.*?)3\. Nach Artikel 143g", gazette))
    before, paragraph = old["115"].split(" (2) ")
    paragraph, heading, next_part = paragraph.partition(" X a. Verteidigungsfall")
    require(heading and not next_part, "unrecognized part after Art. 115")
    sentences = re.split(r"(?<=\.) ", paragraph)
    require(len(sentences) == 8, "unexpected sentence count in Art. 115(2)")
    sentences.insert(3, one(r"Nach Satz 3 wird folgender Satz eingefügt: „(.*?)“", instructions))
    for index, pattern, insertion in [
            (4, r"In dem neuen Satz 5 werden die Wörter „([^“]+)“ durch die Wörter „([^“]+)“ ersetzt\.", False),
            (5, r"In dem neuen Satz 6 werden nach den Wörtern „([^“]+)“ die Wörter „([^“]+)“ eingefügt\.", True),
            (8, r"In dem neuen Satz 9 wird die Angabe „([^“]+)“ durch die Angabe „([^“]+)“ ersetzt\.", False)]:
        matches = re.findall(pattern, instructions)
        require(len(matches) == 1, "unexpected Art. 115 instruction")
        original, updated = matches[0]
        sentences[index] = replace_one(sentences[index], original, original + " " + updated if insertion else updated)
    result["115"] = before + " (2) " + " ".join(sentences) + heading
    inserted = clean(one(r"Nach Artikel 143g wird folgender Artikel 143h eingefügt:\s*„Artikel 143h\s+(.*?)“\s*\n\s*Artikel 2\b", gazette))
    require("143g" in result and "143h" not in result, "unexpected preexisting Art. 143h")
    ordered = {}
    for key, value in result.items():
        ordered[key] = value
        if key == "143g":
            ordered["143h"] = inserted
    return ordered


def audit(directory: Path, review_path: Path, anchor_path: Path):
    review = json.loads(review_path.read_bytes())
    texts = {}
    for name, source in review["sources"].items():
        pdf = directory / source["source_file"]
        require(hashlib.sha256(pdf.read_bytes()).hexdigest() == source["pdf_sha256"],
                f"wrong PDF for {name}")
        raw = subprocess.check_output(["pdftotext", "-layout", str(pdf), "-"]).decode("utf-8")
        texts[name] = re.sub(r"\x0cBundesgesetzblatt Jahrgang[^\n]+\n", "\n", raw)
    require("19. Dezember 2022" in clean(texts["21843"][:1600]), "unexpected earlier federal edition")
    require("22. März 2025" in clean(texts["23183"][:1600]), "unexpected later federal edition")
    older = federal_edition(texts["21843"])
    newest = federal_edition(texts["23183"])
    if "9482" in texts:
        if __package__:
            from .audit_gg_2010 import audit_2010
        else:
            from audit_gg_2010 import audit_2010
        audit_2010(directory, review, texts)
    if "17609" in texts:
        if __package__:
            from .audit_gg_2017 import audit_early
        else:
            from audit_gg_2017 import audit_early
        audit_early(directory, review, federal_edition(texts["17609"]), older, edition_digest)
    gazette24 = texts["2024-439"]
    require("19. Dezember 2022 (BGBl. I S. 2478)" in clean(gazette24[:2300]), "incorrect preceding amendment")
    require("20. Dezember 2024 (BGBl. 2024 I Nr. 439)" in clean(texts["2025-94"][:2300]),
            "incorrect preceding amendment")
    article82 = clean(one(r"wie folgt gefasst:\s*„(.*?)“", texts["2022-2478"]))
    require(older["82"].startswith(article82 + " (2)"), "Art. 82 differs from the initial gazette")
    intermediate = older.copy()
    intermediate["93"] = clean(one(r"„Artikel 93\s+(.*?)\n\s*Artikel 94\s*\n", gazette24))
    intermediate["94"] = clean(one(r"\n\s*Artikel 94\s*\n(.*?)“\s*\n\s*Artikel 2\b", gazette24))
    final = enact_2025(intermediate, texts["2025-94"])
    require(list(final.items()) == list(newest.items()), "gazette replay disagrees with the complete later edition")

    handbook = clean(texts["bundestag-amendments-since-1990"])
    anchor = json.loads(gzip.decompress(anchor_path.read_bytes()))
    canonical = json.dumps(anchor, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    require(hashlib.sha256(canonical).hexdigest() == review["anchor"]["state_sha256"], "wrong GII state")
    current = {n["enbez"]: n["text"] for n in anchor["norms"]}
    for version, data, number, title in zip(review["editions"][-3:], [older, intermediate, final], [67, 68, 69],
                                            ["Artikel 82", "Artikel 93 und 94", "Artikel 109, 115 und 143h"], strict=True):
        require(len(data) == version["printed_blocks"] and edition_digest(data) == version["printed_state_sha256"],
                "reviewed complete printed text mismatch")
        effective = date.fromisoformat(version["effective_from"])
        published = date.fromisoformat(version["published_at"])
        month = {3: "März", 12: "Dezember"}[published.month]
        gazette = clean(texts[version["gazette"]])
        require(f"am {published.day}. {month} {published.year}" in gazette[:400]
                and effective == published + timedelta(days=1)
                and "Dieses Gesetz tritt am Tag nach der Verkündung in Kraft." in gazette,
                "publication date or commencement clause mismatch")
        require(f"[{number}.] Gesetz zur Änderung des Grundgesetzes ({title})" in handbook
                and f"Inkrafttreten am {effective.day}.{effective.month}.{effective.year}" in handbook,
                "Bundestag amendment chronology mismatch")
        for edit in version["changes_from_previous"] if number > 67 else []:
            label = edit["enbez"].removeprefix("Art ")
            full_current = newest[label]
            if label == "115":
                full_current = full_current.partition(" X a. Verteidigungsfall")[0]
            require("".join(full_current.split()) == "".join(current[edit["enbez"]].split()),
                    f"GII wording differs in Art. {label}")
            expected = older.get(label)
            if expected is not None and label == "115":
                expected = expected.partition(" X a. Verteidigungsfall")[0]
            require(expected == edit["previous_text"], f"old wording differs in Art. {label}")
    return [(v["effective_from"], v["printed_state_sha256"]) for v in review["editions"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, required=True)
    parser.add_argument("--review", type=Path, default=REVIEW)
    parser.add_argument("--anchor", type=Path, default=FIXTURE)
    args = parser.parse_args()
    print(json.dumps(audit(args.sources, args.review, args.anchor), indent=2))
