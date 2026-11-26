"""Reapply the 2017–2022 Basic Law amendment sequence to the complete German federal edition."""
from __future__ import annotations

from datetime import date, timedelta
import hashlib
import re
import subprocess


def _replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"expected one source passage: {old!r}")
    return text.replace(old, new)


def _after(blocks, key, label, wording):
    if key not in blocks or label in blocks:
        raise ValueError(f"invalid insertion of {label} after {key}")
    result = {}
    for article, body in blocks.items():
        result[article] = body
        if article == key:
            result[label] = wording
    return result


def _operative(blocks):
    result = blocks.copy()
    for article in ("82", "100"):
        # The federal edition places editorial case notes and chapter titles
        # in the page flow. The two footnotes changed independently of the law.
        result[article] = result[article].split(" Fußnote")[0].removesuffix(
            " VIII. Die Ausführung der Bundesgesetze und die Bundesverwaltung")
    return result


def _gazette(directory, source):
    path = directory / source["source_file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != source["pdf_sha256"]:
        raise ValueError(f"wrong gazette: {path.name}")
    raw = subprocess.check_output(["pdftotext", "-raw", str(path), "-"]).decode()
    # In these issues a printed hyphen before a wrapped lowercase suffix
    # divides a word. Retain internal hyphens and explicit connecting hyphens.
    joined = re.sub(r"(?<=[A-Za-zÄÖÜäöüß])[-\u00ad]\s*\n\s*(?=[a-zäöüß])", "", raw)
    return " ".join(joined.replace("\u00ad", "").split())


def _apply(ordinal, current, quoted):
    if ordinal == 63:
        if len(quoted) != 9 or quoted[1:3] != ["Die Mittel", "Sie"]:
            raise ValueError("unexpected Art. 104b commands")
        current["104b"] = _replace(current["104b"], "Die Mittel sind befristet",
                                   quoted[0] + " " + quoted[2] + " sind befristet")
        if not quoted[3].startswith("Artikel 104c ") or not quoted[4].startswith("Artikel 104d "):
            raise ValueError("unexpected Art. 104c/d commands")
        current["104c"] = quoted[3].removeprefix("Artikel 104c ")
        current = _after(current, "104c", "104d", quoted[4].removeprefix("Artikel 104d "))
        current["125c"] = _replace(current["125c"], " " + quoted[5], "") + " " + quoted[6] + " " + quoted[7]
        current["143e"] += " " + quoted[8]
    elif ordinal == 64:
        if len(quoted) != 5:
            raise ValueError("unexpected Grundsteuer commands")
        boundary = "die Hochschulzulassung und die Hochschulabschlüsse"
        current["72"] = _replace(current["72"], boundary + ".", boundary + "; " + quoted[0])
        current["105"] = _replace(current["105"], "(2) " + quoted[2] + " hat",
                                  "(2) " + quoted[1] + " " + quoted[3] + " hat")
        current["125b"] += " " + quoted[4]
    elif ordinal == 65:
        if len(quoted) != 2 or not quoted[1].startswith("Artikel 143h "):
            raise ValueError("unexpected compensation provision")
        current["104a"] = _replace(current["104a"], " (4)", " " + quoted[0] + " (4)")
        current = _after(current, "143g", "143h", quoted[1].removeprefix("Artikel 143h "))
    elif ordinal == 66:
        if len(quoted) != 1 or not quoted[0].startswith("(1a) "):
            raise ValueError("unexpected Art. 87a command")
        current["87a"] = _replace(current["87a"], " (2)", " " + quoted[0] + " (2)")
    elif ordinal == 67:
        if len(quoted) != 1 or not quoted[0].startswith("(1) ") or current["82"].count(" (2) ") != 1:
            raise ValueError("unexpected Art. 82 command")
        current["82"] = quoted[0] + " (2) " + current["82"].split(" (2) ", 1)[1]
    return current


def _check_custodian(directory, review, current):
    from bs4 import BeautifulSoup

    for year, start, end in [("2020", "08.10.2020", "31.12.2020"),
                             ("2021", "01.01.2021", "24.03.2025")]:
        source = review["expiry_confirmations"][year]
        data = (directory / source["source_file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != source["html_sha256"]:
            raise ValueError("wrong DRV source")
        body = BeautifulSoup(data, "html.parser").select_one(".documentContent")
        text = " ".join(body.get_text(" ", strip=True).split())
        if f"Inkrafttreten {start}" not in text or f"Gültig bis {end}" not in text:
            raise ValueError("DRV validity dates disagree")
        if year == "2020":
            legal = " ".join(" ".join(n.get_text(" ", strip=True)
                                     for n in body.select("span.changes")).split())
            if legal != current["143h"]:
                raise ValueError("DRV and the gazette differ on Art. 143h")
        elif "(aufgehoben)" not in text:
            raise ValueError("DRV expiry record missing")


def audit_early(directory, review, first, last, edition_digest):
    current = _operative(first)
    editions = {row["effective_from"]: row for row in review["editions"]}
    original = ["2017-07-20", "2019-04-04", "2019-11-21", "2020-10-08",
                "2022-07-01", "2022-12-24"]
    snapshots = {}
    for (ordinal, day) in zip(range(62, 68), original, strict=True):
        ed = editions[day]
        text = _gazette(directory, review["sources"][ed["gazette"]])
        pub = date.fromisoformat(ed["published_at"])
        months = {4: "April", 6: "Juni", 7: "Juli", 10: "Oktober", 11: "November", 12: "Dezember"}
        print_date = f"am {pub.day}. {months[pub.month]} {pub.year}"
        if (print_date not in text or "Dieses Gesetz tritt am Tag nach der Verkündung in Kraft." not in text
                or pub + timedelta(days=1) != date.fromisoformat(day)):
            raise ValueError(f"wrong publication or operative date for {day}")
        if ordinal == 62:
            companion = _gazette(directory, review["sources"]["gazette-61"])
            if print_date not in companion or "Dieses Gesetz tritt am Tag nach der Verkündung in Kraft." not in companion:
                raise ValueError("the companion July 2017 amendment has a different boundary")
        else:
            current = _apply(ordinal, current, re.findall("„([^“]+)“", text))
        snapshots[day] = current.copy()
        if ordinal == 65:
            _check_custodian(directory, review, current)
            if "Artikel 143h des Grundgesetzes tritt am 31. Dezember 2020 außer Kraft." not in text:
                raise ValueError("missing expiry of Art. 143h")
            current.pop("143h")
            snapshots["2021-01-01"] = current.copy()
    if list(current.items()) != list(_operative(last).items()):
        raise ValueError("the 2017 gazette replay differs from the complete 2022 federal edition")
    for day, snapshot in snapshots.items():
        ed = editions[day]
        if day != "2022-12-24" and (len(snapshot) != ed["printed_blocks"]
                                    or edition_digest(snapshot) != ed["printed_state_sha256"]):
            raise ValueError(f"unexpected full-edition digest for {day}")
    # The 2022 edition also anchors the contemporary audit with its page notes.
    # Link every changed operative article back to the reviewed CAS predecessor.
    pairs = list(snapshots)
    for previous, following in zip(pairs, pairs[1:]):
        before, after = snapshots[previous], snapshots[following]
        ed = editions[following]
        changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
        if changed != {c["enbez"].removeprefix("Art ") for c in ed["changes_from_previous"]}:
            raise ValueError(f"unexpected changed articles on {following}")
        for change in ed["changes_from_previous"]:
            number = change["enbez"].removeprefix("Art ")
            text = (change["previous_norm"]["text"] if "previous_norm" in change else change["previous_text"])
            if text != before.get(number):
                raise ValueError(f"incorrect prior wording for Art. {number} on {previous}")
