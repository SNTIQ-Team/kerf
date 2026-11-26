"""Replay the 2010–2017 federal Basic Law editions and reconcile their original gazettes."""

from __future__ import annotations

from datetime import date, timedelta
import re
import subprocess

if __package__:
    from .audit_gg_editions import edition_digest, federal_edition, replace_one, require
else:
    from audit_gg_editions import edition_digest, federal_edition, replace_one, require


def read_gazette(directory, source, published_on, effective_on):
    file = directory / source["source_file"]
    raw = subprocess.check_output(["pdftotext", "-raw", str(file), "-"]).decode()
    published = date.fromisoformat(published_on)
    day = date.fromisoformat(effective_on)
    month = {7: "Juli", 12: "Dezember"}[published.month]
    require(f"am {published.day}. {month} {published.year}" in raw, "missing original publication date")
    require(day == published + timedelta(days=1), "Bundestag chronology disagrees")
    # Strip only the gazette page signature and footer at page ends.
    body = re.sub(
        r"(?m)^\d+ Bundesgesetzblatt Jahrgang[^\n]+\nDas Bundesgesetzblatt im Internet:[^\f]+\f",
        "\n",
        raw,
    )
    # Two compound names retain a real hyphen even when physically divided here.
    body = body.replace("Öffentlich-\nPrivaten", "Öffentlich-Privaten").replace(
        "Schleswig-\nHolstein", "Schleswig-Holstein"
    )
    body = re.sub(r"(?<=[\w])[-\u00ad][ \t]*\n[ \t]*(?=[a-zäöüß])", "", body)
    body = " ".join(body.replace("\u00ad", "").split())
    require(
        "Dieses Gesetz tritt am Tag nach der Verkündung in Kraft." in body,
        "missing commencement clause",
    )
    return body, re.findall("„([^“]+)“", body)


def federal_text(key, raw):
    if key == "9482":
        pages = [page for page in raw.split("\f") if page.strip()]
        for index, page in enumerate(pages, 1):
            # Every page has a printed “- Nº -” footer; require it at the bottom.
            footer = r"\n[ \t]*[-\u00ad][ \t]*" + str(index) + r"[ \t]*[-\u00ad][ \t]*\s*\Z"
            require(re.search(footer, page) is not None, f"unexpected 2010 footer on p.{index}")
            pages[index - 1] = re.sub(footer, "\n", page)
        raw = "\f".join(pages)
    return federal_edition(raw)


def excerpt(block, first, after):
    prefix, sep, rest = block.partition(first)
    require(sep != "", f"boundary missing: {first}")
    paragraph, sep, suffix = rest.partition(after)
    require(sep != "", f"boundary missing: {after}")
    return prefix, first + paragraph, sep + suffix


def insert_after(blocks, previous, key, value):
    require(previous in blocks and key not in blocks, f"cannot insert {key}")
    out = {}
    for k, v in blocks.items():
        out[k] = v
        if k == previous:
            out[key] = value
    return out


def apply_2017(old, party, quotes):
    require(len(party) == 1 and len(quotes) == 25, "different amendment command count")
    result = old.copy()
    result["21"] = replace_one(
        result["21"],
        "Über die Frage der Verfassungswidrigkeit entscheidet das Bundesverfassungsgericht. (3)",
        party[0] + " (5)",
    )
    _, paragraph_two, paragraph_three = excerpt(result["90"], "(2) ", " (3) ")
    paragraph_two = replace_one(paragraph_two, "(2) ", "(3) ")
    paragraph_two = replace_one(paragraph_two, quotes[2] + " " + quotes[3], quotes[2])
    paragraph_three = replace_one(paragraph_three, "(3) ", "(4) ")
    paragraph_three = replace_one(
        paragraph_three, quotes[4] + " " + quotes[5], quotes[4] + " " + quotes[6]
    )
    paragraph_three = replace_one(
        paragraph_three, quotes[7] + " " + quotes[8], quotes[7] + " " + quotes[9]
    )
    result["90"] = " ".join((quotes[0], quotes[1], paragraph_two.strip(), paragraph_three.strip()))
    result["91c"] += " " + quotes[10]
    result["104b"] = replace_one(
        result["104b"], "geregelt. Die Mittel", "geregelt. " + quotes[11] + " Die Mittel"
    )
    require(quotes[12].startswith("Artikel 104c "), "wrong inserted article")
    result = insert_after(result, "104b", "104c", quotes[12].removeprefix("Artikel 104c "))
    result["107"] = (
        result["107"].split(" Der Länderanteil am Aufkommen der Umsatzsteuer ")[0]
        + " "
        + quotes[13]
        + " "
        + quotes[14]
    )
    result["108"] = replace_one(result["108"], " (5) ", " " + quotes[15] + " " + quotes[16] + " (5) ")
    result["109a"] = (
        "(1) " + result["109a"].split(" Die Beschlüsse des Stabilitätsrats ")[0] + " " + quotes[17]
    )
    result["114"] = replace_one(
        result["114"], quotes[18] + ".", quotes[18] + " " + quotes[19] + ". " + quotes[20]
    )
    result["125c"] = (
        result["125c"].split(" Die im Bereich der Gemeindeverkehrsfinanzierung ")[0]
        + " "
        + quotes[21]
        + " "
        + quotes[22]
    )
    result["143d"] += " " + quotes[23]
    parts = re.split(r"Artikel (143[efg]) ", quotes[24])
    require(parts[0] == "" and parts[1::2] == ["143e", "143f", "143g"], "wrong new articles")
    for prev, number, value in zip(("143d", "143e", "143f"), parts[1::2], parts[2::2], strict=True):
        result = insert_after(result, prev, number, value.strip())
    return result


def normalize_2012(earlier, later):
    # PDF letterspacing and three editorial repealed markers do not change the wording.
    key = "Eingangsformel und Präambel"
    require("".join(earlier[key].split()) == "".join(later[key].split()), "preamble differs")
    earlier[key] = later[key]
    for k in ("49", "59a", "142a"):
        require(
            earlier[k].startswith("-") and later[k] == "(weggefallen)" + earlier[k][1:],
            "changed repeal marker",
        )
        earlier[k] = later[k]
    # Match the digit's punctuation in the independent Bundestag paper edition, p. 224.
    earlier["106"] = replace_one(earlier["106"], "3 die Verkehrsteuern", "3. die Verkehrsteuern")


def compare_2010(raw, current, gazettes, brochure, originals):
    """Resolve three typesetter discrepancies against the original enacted texts."""
    original = raw.copy()
    for number, before, after in [
        ("23", "die Stellungnahme des Bundestages", "die Stellungnahmen des Bundestages"),
        ("26", "Zur Kriegsführung", "Zur Kriegführung"),
        ("33", "öffentlichrechtlichen Dienst-", "öffentlich-rechtlichen Dienst-"),
        (
            "95",
            "der Arbeitsund der Sozialgerichtsbarkeit",
            "der Arbeits- und der Sozialgerichtsbarkeit",
        ),
        ("114", "der Haushaltsund Wirtschaftsführung", "der Haushalts- und Wirtschaftsführung"),
        ("Anhang EV", "890 - 892, sieht folgende", "890 - 892, - sieht folgende"),
    ]:
        original[number] = replace_one(original[number], before, after)
        if number in ("95", "114"):
            require(
                after in brochure,
                f"Bundestag corroboration missing for the joining hyphen in Art. {number}",
            )
    master_text = originals["gg-gazette-1949"].split("\f")
    require("Zur Kriegführung bestimmte Waffen" in master_text[3], "1949 printed Art. 26 differs")
    require(
        "öffentlich-rechtlichen Dienst- und Treuever" in master_text[4], "1949 printed Art. 33 differs"
    )
    # The scanned 1992 Art. 23(3), p. 2086, prints the plural. The later
    # independently typeset Bundestag edition also prints this exact sentence.
    require(
        "berücksichtigt die Stellungnahmen des Bundestages bei den Verhandlungen." in brochure,
        "Bundestag corroboration of the plural in Art. 23 missing",
    )
    for number in ("Eingangsformel und Präambel", "12a", "118", "138", "140", "Anhang EV"):
        require(
            "".join(original[number].split()) == "".join(current[number].split()),
            f"letter spacing differs beyond layout in {number}",
        )
        original[number] = current[number]
    require(
        original["91e"].removesuffix(" IX. Die Rechtsprechung")
        == gazettes[58][1][0].removeprefix("Artikel 91e "),
        "Art. 91e differs from the 2010 gazette",
    )
    reviewed = original.copy()
    reviewed["93"] = replace_one(
        reviewed["93"], "erhoben werden kann; 5.", "erhoben werden kann; " + gazettes[59][1][0] + " 5."
    )
    require(
        list(reviewed.items()) == list(current.items()),
        "2010 replay differs from complete 2012 edition",
    )
    return original


def parliament(raw):
    pages = [p for p in raw.split("\f") if p.strip()]
    text = "\n".join(re.sub(r"(?m)^\s*\d+ Artikel [^\n]+$", "", p).strip() for p in pages[25:])
    # Line-end hyphens in these selected Bundestag passages wrap a lowercase
    # suffix. The permanently hyphenated compound in Art. 33 is checked separately.
    text = re.sub(r"(?<=[A-Za-zÄÖÜäöüß])-[ \t]*\n[ \t]*(?=[a-zäöüß])", "", text)
    return " ".join(text.split())


def audit_2010(directory, review, texts):
    editions = {ed["effective_from"]: ed for ed in review["editions"]}
    chronology = [
        ("2010-07-27", 58),
        ("2012-07-17", 59),
        ("2015-01-01", 60),
        ("2017-07-20", 61),
        ("2017-07-20", 62),
    ]
    originals = {}
    for day, ordinal in chronology:
        source = review["sources"][f"gazette-{ordinal}"]
        originals[ordinal] = read_gazette(directory, source, editions[day]["published_at"], day)
    old = federal_text("14373", texts["14373"])
    latest = federal_text("17609", texts["17609"])
    brochure = parliament(texts["17608"])
    earliest = compare_2010(federal_text("9482", texts["9482"]), old, originals, brochure, texts)
    normalize_2012(earliest, latest)
    normalize_2012(old, latest)
    require(originals[59][1][0] in old["93"], "2012 amendment absent")
    require("Gesetzes vom 21. Juli 2010" in originals[59][0], "2010 predecessor missing")
    require("Gesetzes vom 11. Juli 2012" in originals[60][0], "2012 predecessor missing")
    require("Gesetzes vom 23. Dezember 2014" in originals[61][0], "2014 predecessor missing")
    require("Gesetzes vom 13. Juli 2017" in originals[62][0], "2017 companion missing")
    mid = old.copy()
    require(len(originals[60][1]) == 1 and mid["91b"].count(" (2) ") == 1, "2014 instructions differ")
    mid["91b"] = originals[60][1][0] + " (2) " + mid["91b"].split(" (2) ", 1)[1]
    require(mid["91b"] in brochure, "independent Bundestag text of Art. 91b differs")
    require("2. die Erbschaftsteuer, 3. die Verkehrsteuern" in brochure, "missing Art. 106 marker")
    rebuilt = apply_2017(mid, originals[61][1], originals[62][1])
    # The 2017 BMJ inserts an editorial reference explaining the continued application of old Art. 107.
    note = " Fußnote (+++ Art. 107 in der bis zum 19.7.2017 geltenden Fassung: Zur weiteren Anwendung vgl. Art 143g +++)"
    require(latest["107"].endswith(note), "unexpected note on Art. 107")
    latest["107"] = latest["107"].removesuffix(note)
    require(
        list(rebuilt.items()) == list(latest.items()),
        "2017 replay differs from the complete federal edition",
    )
    states = {"2010-07-27": earliest, "2012-07-17": old, "2015-01-01": mid, "2017-07-20": rebuilt}
    for day in list(states)[:-1]:
        ed = editions[day]
        require(
            len(states[day]) == ed["printed_blocks"]
            and edition_digest(states[day]) == ed["printed_state_sha256"],
            f"wrong edition digest for {day}",
        )
    # Later reviews independently pin the 2017 printing with its editorial footnotes.
    # The wording of every changed article must match the canonical CAS reverse edit.
    for earlier, later in zip(states, list(states)[1:]):
        before, after = states[earlier], states[later]
        changes = {
            c["enbez"].removeprefix("Art "): c["previous_text"]
            for c in editions[later]["changes_from_previous"]
        }
        expected = {
            n: before.get(n) for n in before.keys() | after.keys() if before.get(n) != after.get(n)
        }
        require(changes == expected, f"prior article text differs from the federal source at {later}")
