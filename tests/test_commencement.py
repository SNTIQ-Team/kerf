"""Commencement clauses of real BGBl acts (tests/fixtures/commencement_articles.json).

Every fixture is a verbatim slice of the extracted final BGBl text
(``text_sha256``/``start``/``end`` identify the source); every expectation
below was checked by hand against the printed clause.  ``P`` is the printed
promulgation day of the issue (``printed_promulgation``).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from commencement import (  # noqa: E402
    compute_timing, dip_cross_check, disambiguate_levels, item_path,
    parse_commencement_text, parse_document_commencement, parse_scope,
    resolve_applies_from, resolve_in_force,
)

FIXTURES = {row["document_id"]: row for row in json.loads(
    (Path(__file__).parent / "fixtures" / "commencement_articles.json")
    .read_text(encoding="utf-8"))}
OFFICIAL = "official_commencement_clause"
COMPUTED = "computed_from_commencement_clause"


def parsed(document_id: str) -> dict:
    row = FIXTURES[document_id]
    return parse_document_commencement(row["text"],
                                       legacy=row["era"] == "bgbl.de")


def resolve(document_id: str, target: dict | None = None, **item) -> dict:
    row = FIXTURES[document_id]
    return resolve_in_force(parsed(document_id), item_path(**item),
                            promulgated=row["printed_promulgation"],
                            target=target)


# (document, item, expected date, expected status or "unknown:<reason>")
CASES = [
    # Bürgergeld-Gesetz, BGBl. 2022 I S. 2328, Art. 13 (SGB II) -- lists at
    # every level incl. Dreifachbuchstabe and a collective Artikel 12.
    ("bgbl-1-2022-s2328", dict(article="1", nummer="2", buchstabe="a"),
     "2023-07-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="2", buchstabe="d"),
     "2023-01-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="3a"), "2023-07-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="10"), "2023-07-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="12"), "2023-01-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="15"), "2023-07-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="32", buchstabe="a",
                               doppelbuchstabe="aa"), "2023-07-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="32", buchstabe="a",
                               doppelbuchstabe="bb"), "2023-01-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="1", nummer="18"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2022-s2328", dict(article="12", absatz="23", nummer="2"),
     "2024-01-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="12", absatz="1", nummer="2"),
     "2023-07-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="12", absatz="1", nummer="1"),
     "2023-01-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="12", absatz="3"), "2023-07-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="12", absatz="14", nummer="1",
                               buchstabe="a", doppelbuchstabe="aa",
                               dreifachbuchstabe="ccc"),
     "2023-07-01", OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="5", nummer="12"), "2024-04-01",
     OFFICIAL),
    ("bgbl-1-2022-s2328", dict(article="11"), "2023-07-01", OFFICIAL),
    # Fachkräfteeinwanderung, BGBl. 2023 I Nr. 217, Art. 12 (AufenthG):
    # "mit Ausnahme von …", "frühestens jedoch am 18. November 2023".
    ("bgbl-1-2023-217", dict(article="1", nummer="18", buchstabe="b"),
     "2023-08-19", COMPUTED),
    ("bgbl-1-2023-217", dict(article="1", nummer="18", buchstabe="a"),
     "2023-11-18", COMPUTED),
    ("bgbl-1-2023-217", dict(article="1", nummer="5"), "2023-11-18",
     COMPUTED),
    ("bgbl-1-2023-217", dict(article="1", nummer="30"), "2023-08-19",
     COMPUTED),
    ("bgbl-1-2023-217", dict(article="1"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2023-217", dict(article="2", nummer="18"), "2024-06-02",
     OFFICIAL),
    ("bgbl-1-2023-217", dict(article="2", nummer="3"), "2024-03-01",
     OFFICIAL),
    ("bgbl-1-2023-217", dict(article="7", nummer="1"), "2026-01-01",
     OFFICIAL),
    ("bgbl-1-2023-217", dict(article="7", nummer="2"), "2023-08-19",
     COMPUTED),
    ("bgbl-1-2023-217", dict(article="7a"), "2023-08-19", COMPUTED),
    ("bgbl-1-2023-217", dict(article="10"), "2024-03-01", OFFICIAL),
    # BGBl. 2023 I Nr. 408, Art. 21: Artikel 3 Nummer 6 is printed in both
    # Absatz 2 (1. Juli 2023) and Absatz 4 (Tag nach der Verkündung).
    ("bgbl-1-2023-408", dict(article="3", nummer="6"), None,
     "unknown:conflicting_rules"),
    ("bgbl-1-2023-408", dict(article="3", nummer="4"), "2023-07-01",
     OFFICIAL),
    ("bgbl-1-2023-408", dict(article="13a", nummer="2"), "2023-12-29",
     COMPUTED),
    ("bgbl-1-2023-408", dict(article="13a", nummer="1"), "2025-01-01",
     OFFICIAL),
    ("bgbl-1-2023-408", dict(article="13", nummer="4"), "2023-12-29",
     COMPUTED),
    ("bgbl-1-2023-408", dict(article="13", nummer="5"), "2024-01-01",
     OFFICIAL),
    ("bgbl-1-2023-408", dict(article="7", nummer="1", buchstabe="b"),
     "2024-01-01", OFFICIAL),
    ("bgbl-1-2023-408", dict(article="12"), "2023-12-29", COMPUTED),
    # Steueränderungsgesetz 2025, BGBl. 2025 I Nr. 363, Art. 12.
    ("bgbl-1-2025-363", dict(article="1"), "2025-12-24", COMPUTED),
    ("bgbl-1-2025-363", dict(article="3"), "2025-12-24", COMPUTED),
    ("bgbl-1-2025-363", dict(article="5"), "2026-01-01", OFFICIAL),
    # 13. SGB II-ÄndG, BGBl. 2026 I Nr. 107, Art. 12.
    ("bgbl-1-2026-107", dict(article="1", nummer="26", buchstabe="d"),
     "2026-04-23", COMPUTED),
    ("bgbl-1-2026-107", dict(article="1", nummer="26", buchstabe="a"),
     "2026-07-01", OFFICIAL),
    ("bgbl-1-2026-107", dict(article="1", nummer="14", buchstabe="a"),
     "2027-08-01", OFFICIAL),
    ("bgbl-1-2026-107", dict(article="1a"), "2029-01-01", OFFICIAL),
    ("bgbl-1-2026-107", dict(article="2", nummer="1", buchstabe="c"),
     "2027-08-01", OFFICIAL),
    ("bgbl-1-2026-107", dict(article="2", nummer="4"), "2026-07-01",
     OFFICIAL),
    ("bgbl-1-2026-107", dict(article="3", nummer="2"), "2026-04-23",
     COMPUTED),
    # Asylverfahrensbeschleunigungsgesetz, BGBl. I 2015 S. 1722 (AsylbLG,
    # AsylG, AufenthG): letter-spaced "( 2 )A r t i k e l2N u m m e r1 1…".
    ("bgbl-1-2015-s1722", dict(article="2", nummer="11"), "2016-01-01",
     OFFICIAL),
    ("bgbl-1-2015-s1722", dict(article="13"), "2016-01-01", OFFICIAL),
    ("bgbl-1-2015-s1722", dict(article="2", nummer="10"), "2015-10-24",
     COMPUTED),
    ("bgbl-1-2015-s1722", dict(article="11", nummer="2"), "2016-11-01",
     OFFICIAL),
    ("bgbl-1-2015-s1722", dict(article="4"), "2015-11-01", OFFICIAL),
    # AsylbLG 2019 (BGBl. I S. 1290): single default date.
    ("bgbl-1-2019-s1290", dict(article="1"), "2019-09-01", OFFICIAL),
    # BGBl. I 2021 S. 5162 (AsylbLG/SGB XII): article range, retroactive.
    ("bgbl-1-2021-s5162", dict(article="17"), "2021-11-25", OFFICIAL),
    ("bgbl-1-2021-s5162", dict(article="12a"), "2022-01-01", OFFICIAL),
    ("bgbl-1-2021-s5162", dict(article="1"), "2021-12-12", COMPUTED),
    # BGBl. 2024 I Nr. 152 (AsylG/AufenthG/AZRG): deep lists and ranges.
    ("bgbl-1-2024-152", dict(article="13", nummer="1", buchstabe="c"),
     "2024-03-01", OFFICIAL),
    ("bgbl-1-2024-152", dict(article="1", nummer="5", buchstabe="b",
                             doppelbuchstabe="bb"), "2024-11-01", OFFICIAL),
    ("bgbl-1-2024-152", dict(article="1", nummer="5", buchstabe="b",
                             doppelbuchstabe="ee"), "2025-11-01", OFFICIAL),
    ("bgbl-1-2024-152", dict(article="1", nummer="5", buchstabe="a",
                             doppelbuchstabe="aa"), "2024-05-16", COMPUTED),
    ("bgbl-1-2024-152", dict(article="2", nummer="5", buchstabe="f",
                             doppelbuchstabe="aa", dreifachbuchstabe="bbb"),
     "2026-11-01", OFFICIAL),
    ("bgbl-1-2024-152", dict(article="9"), "2024-11-01", OFFICIAL),
    ("bgbl-1-2024-152", dict(article="1", nummer="29"), "2025-11-01",
     OFFICIAL),
    # Richtlinienumsetzungsgesetz 2007 (BGBl. I S. 1970, AufenthG/AsylVfG).
    ("bgbl-1-2007-s1970", dict(article="1", nummer="59", buchstabe="b"),
     "2008-05-01", OFFICIAL),
    ("bgbl-1-2007-s1970", dict(article="1", nummer="59", buchstabe="a"),
     "2009-02-01", OFFICIAL),
    ("bgbl-1-2007-s1970", dict(article="1", nummer="1"), "2007-08-28",
     COMPUTED),
    ("bgbl-1-2007-s1970", dict(article="5", nummer="7", buchstabe="a"),
     None, "unknown:item_split_across_rules"),
    ("bgbl-1-2007-s1970", dict(article="5", nummer="7", buchstabe="b"),
     "2007-08-28", COMPUTED),
    # AufenthG 2008 (BGBl. I S. 2846): "Dieses Gesetz tritt mit Ausnahme von
    # Artikel 1 Nr. 10 am 1. Januar 2009 in Kraft."
    ("bgbl-1-2008-s2846", dict(article="1", nummer="10"), "2008-12-25",
     COMPUTED),
    ("bgbl-1-2008-s2846", dict(article="1", nummer="9"), "2009-01-01",
     OFFICIAL),
    # AufenthG 2011 (BGBl. I S. 3037): "am ersten Tag des 18. auf die
    # Verkündung folgenden Monats" (Dezember 2011 + 18 Monate).
    ("bgbl-1-2011-s3037", dict(article="1"), "2013-06-01", COMPUTED),
    # BGBl. I 2019 S. 1626: letter-spaced "mit Wirkung v o m1 .J a n u a r…".
    ("bgbl-1-2019-s1626", dict(article="74", nummer="2", buchstabe="b"),
     "2019-01-01", OFFICIAL),
    ("bgbl-1-2019-s1626", dict(article="74", nummer="2", buchstabe="a"),
     "2019-11-26", COMPUTED),
    ("bgbl-1-2019-s1626", dict(article="127"), "2019-01-01", OFFICIAL),
    ("bgbl-1-2019-s1626", dict(article="70", nummer="11", buchstabe="a",
                               doppelbuchstabe="dd"), "2020-01-01", OFFICIAL),
    # AsylG/AufenthG 2022 (BGBl. I S. 2817): first day of the next quarter.
    ("bgbl-1-2022-s2817", dict(article="1"), "2023-01-01", COMPUTED),
    # Asylpaket II (BGBl. I 2016 S. 390): old layout "am T ag nach".
    ("bgbl-1-2016-s390", dict(article="2"), "2016-03-17", COMPUTED),
    # BGBl. 2024 I Nr. 236 (AsylG/VwGO): "am Tag nach seiner Verkündung".
    ("bgbl-1-2024-236", dict(article="5", nummer="2"), "2025-01-01",
     OFFICIAL),
    ("bgbl-1-2024-236", dict(article="5", nummer="1"), "2024-07-19",
     COMPUTED),
    ("bgbl-1-2024-236", dict(article="19"), "2024-07-19", COMPUTED),
    ("bgbl-1-2024-236", dict(article="36", nummer="5"), "2025-01-01",
     OFFICIAL),
    # BGB 2013 (BGBl. I S. 273): tied to a treaty, Bekanntmachung.
    ("bgbl-1-2013-s273", dict(article="1", nummer="1"), None,
     "unknown:bekanntmachung_dependent"),
    ("bgbl-1-2013-s273", dict(article="1", nummer="9"), "2013-02-26",
     COMPUTED),
    ("bgbl-1-2013-s273", dict(article="3"), "2013-03-01", OFFICIAL),
    # BGB 2021 (BGBl. I S. 3436): enumerated list incl. "in Artikel 1
    # Nummer 3 § 707d des Bürgerlichen Gesetzbuchs".
    ("bgbl-1-2021-s3436", dict(article="1", nummer="3"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2021-s3436", dict(article="1", nummer="2"), "2024-01-01",
     OFFICIAL),
    ("bgbl-1-2021-s3436", dict(article="45", nummer="4", buchstabe="c",
                               doppelbuchstabe="aa"), "2021-08-18", COMPUTED),
    ("bgbl-1-2021-s3436", dict(article="45", nummer="4", buchstabe="c",
                               doppelbuchstabe="bb"), "2024-01-01", OFFICIAL),
    ("bgbl-1-2021-s3436", dict(article="62"), "2021-08-18", COMPUTED),
    # BGB/ZPO 2022 (BGBl. I S. 959).
    ("bgbl-1-2022-s959", dict(article="4", nummer="2"), "2022-07-01",
     COMPUTED),
    ("bgbl-1-2022-s959", dict(article="5", nummer="2"), "2023-01-01",
     OFFICIAL),
    ("bgbl-1-2022-s959", dict(article="19"), "2022-08-01", OFFICIAL),
    ("bgbl-1-2022-s959", dict(article="9"), "2022-07-01", OFFICIAL),
    # ZPO 2009 (BGBl. I S. 2258): "in Nr. 7 § 802k Abs. 3 und 4, Nr. 14a".
    ("bgbl-1-2009-s2258", dict(article="1", nummer="14a"), "2009-08-01",
     COMPUTED),
    ("bgbl-1-2009-s2258", dict(article="1", nummer="7"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2009-s2258", dict(article="1", nummer="3"), "2013-01-01",
     OFFICIAL),
    # ZPO Musterfeststellungsklage (BGBl. I 2018 S. 1151).
    ("bgbl-1-2018-s1151", dict(article="1"), "2018-07-18", COMPUTED),
    ("bgbl-1-2018-s1151", dict(article="2", nummer="1"), "2018-11-01",
     OFFICIAL),
    ("bgbl-1-2018-s1151", dict(article="2", nummer="5"), None,
     "unknown:item_split_across_rules"),
    # ZPO 2016 (BGBl. I S. 2591): letter-spaced "1. Ja- n u a r2 0 1 8i nK r
    # a f t", "Gleichzeitig tritt …", "mit Wirkung zum".
    ("bgbl-1-2016-s2591", dict(article="2"), "2018-01-01", OFFICIAL),
    ("bgbl-1-2016-s2591", dict(article="3"), "2022-01-01", OFFICIAL),
    ("bgbl-1-2016-s2591", dict(article="16"), "2012-04-01", OFFICIAL),
    ("bgbl-1-2016-s2591", dict(article="1", nummer="5"), "2016-11-26",
     COMPUTED),
    ("bgbl-1-2016-s2591", dict(article="1", nummer="16", buchstabe="c",
                               doppelbuchstabe="bb"), "2017-11-01", OFFICIAL),
    ("bgbl-1-2016-s2591", dict(article="1", nummer="16", buchstabe="a"),
     "2017-01-18", OFFICIAL),
    ("bgbl-1-2016-s2591", dict(article="14", nummer="4"), "2018-01-01",
     OFFICIAL),
    # BGB/ZPO 2013 (BGBl. I S. 831): event dependent + Bekanntmachung.
    ("bgbl-1-2013-s831", dict(article="1", nummer="27"), None,
     "unknown:bekanntmachung_dependent"),
    ("bgbl-1-2013-s831", dict(article="1", nummer="26"), "2013-04-25",
     COMPUTED),
    # SGB XII 2019 (BGBl. I S. 473): no whole-act default at all.
    ("bgbl-1-2019-s473", dict(article="5", nummer="2"), "2019-04-26",
     COMPUTED),
    ("bgbl-1-2019-s473", dict(article="5", nummer="1"), "2019-01-01",
     OFFICIAL),
    ("bgbl-1-2019-s473", dict(article="6"), "2020-01-01", OFFICIAL),
    ("bgbl-1-2019-s473", dict(article="5", nummer="3"), None,
     "unknown:no_rule_covers_item"),
    # SGB II 2006 (BGBl. I S. 558): "…, soweit in den folgenden Absätzen
    # nichts Abweichendes be- stimmt ist."
    ("bgbl-1-2006-s558", dict(article="1", nummer="2", buchstabe="b"),
     "2006-07-01", OFFICIAL),
    ("bgbl-1-2006-s558", dict(article="1", nummer="2", buchstabe="a"),
     "2006-04-01", OFFICIAL),
    ("bgbl-1-2006-s558", dict(article="1", nummer="5"), "2006-07-01",
     OFFICIAL),
    ("bgbl-1-2006-s558", dict(article="2"), "2007-01-01", OFFICIAL),
    # SGB II 2007 (BGBl. I S. 2326): whole-act date before promulgation.
    ("bgbl-1-2007-s2326", dict(article="1", nummer="4"), "2007-06-01",
     OFFICIAL),
    ("bgbl-1-2007-s2326", dict(article="1", nummer="1"), "2007-10-01",
     OFFICIAL),
    # SGB II 2016 (BGBl. I S. 1824): collective Artikel 3 Absätze.
    ("bgbl-1-2016-s1824", dict(article="1", nummer="1", buchstabe="c"),
     "2017-01-01", OFFICIAL),
    ("bgbl-1-2016-s1824", dict(article="1", nummer="34", buchstabe="a"),
     "2016-08-01", COMPUTED),
    ("bgbl-1-2016-s1824", dict(article="3", absatz="7", nummer="2"),
     "2016-07-01", OFFICIAL),
    ("bgbl-1-2016-s1824", dict(article="3", absatz="7", nummer="1"),
     "2017-01-01", OFFICIAL),
    ("bgbl-1-2016-s1824", dict(article="3", absatz="9", nummer="1"),
     "2017-01-01", OFFICIAL),
    ("bgbl-1-2016-s1824", dict(article="3a"), "2016-06-01", OFFICIAL),
    # SGB XII 2021 (BGBl. I S. 3932): (1a)/(2a) paragraphs and
    # "Gleichzeitig treten Artikel 2 Nummer 1 bis 32 …".
    ("bgbl-1-2021-s3932", dict(article="19a"), "2020-01-01", OFFICIAL),
    ("bgbl-1-2021-s3932", dict(article="5", nummer="3a"), "2021-01-01",
     OFFICIAL),
    ("bgbl-1-2021-s3932", dict(article="2", nummer="5"), "2021-10-01",
     OFFICIAL),
    ("bgbl-1-2021-s3932", dict(article="89a"), "2021-10-01", OFFICIAL),
    ("bgbl-1-2021-s3932", dict(article="75"), "2021-09-01", COMPUTED),
    ("bgbl-1-2021-s3932", dict(article="4"), "2025-01-01", OFFICIAL),
    # AufenthG/VwVfG 2013 (BGBl. I S. 2749): "§ 3a Absatz 2 Satz 4 Nummer 2
    # und 3 des VwVfG" are units of the amended law, not items.
    ("bgbl-1-2013-s2749", dict(article="2", nummer="3"), "2014-07-01",
     OFFICIAL),
    ("bgbl-1-2013-s2749", dict(article="3", nummer="1"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2013-s2749", dict(article="3", nummer="2"), "2013-08-01",
     COMPUTED),
    # SGB II 2010 (BGBl. I S. 1112): "Buch- s t a b ea ,b ,i …, 1 8b i s2 1".
    ("bgbl-1-2010-s1112", dict(article="1", nummer="1", buchstabe="a"),
     "2010-08-11", COMPUTED),
    ("bgbl-1-2010-s1112", dict(article="1", nummer="1", buchstabe="c"),
     "2011-01-01", OFFICIAL),
    ("bgbl-1-2010-s1112", dict(article="1", nummer="19"), "2010-08-11",
     COMPUTED),
    ("bgbl-1-2010-s1112", dict(article="1", nummer="22"), "2011-01-01",
     OFFICIAL),
    # Hebammenreformgesetz 2019 (BGBl. I S. 1759): "§ 71 Absatz 2 Satz 2"
    # continues the sentence "In Artikel 1 tritt § 71 …".
    ("bgbl-1-2019-s1759", dict(article="2"), "2019-01-01", OFFICIAL),
    ("bgbl-1-2019-s1759", dict(article="1", para="71"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2019-s1759", dict(article="1", para="5"), "2020-01-01",
     OFFICIAL),
    # BGBl. I 2022 S. 760.
    ("bgbl-1-2022-s760", dict(article="4", nummer="1", buchstabe="a"),
     "2022-05-31", OFFICIAL),
    ("bgbl-1-2022-s760", dict(article="4", nummer="1", buchstabe="b"),
     "2022-06-01", OFFICIAL),
    ("bgbl-1-2022-s760", dict(article="7"), "2022-05-28", COMPUTED),
    # BGBl. I 2012 S. 2745.
    ("bgbl-1-2012-s2745", dict(article="1", nummer="1"), "2013-09-01",
     OFFICIAL),
    ("bgbl-1-2012-s2745", dict(article="1", nummer="20"), "2012-12-28",
     COMPUTED),
    # AufenthG 2008 (BGBl. I S. 2965): two dates in one sentence, no default.
    ("bgbl-1-2008-s2965", dict(article="1", nummer="5"), "2009-04-29",
     OFFICIAL),
    ("bgbl-1-2008-s2965", dict(article="1a"), "2008-12-30", COMPUTED),
    ("bgbl-1-2008-s2965", dict(article="1", nummer="6"), None,
     "unknown:no_rule_covers_item"),
    # SGB II 2014 (BGBl. I S. 1306).
    ("bgbl-1-2014-s1306", dict(article="1", nummer="1", buchstabe="a"),
     "2009-01-01", OFFICIAL),
    ("bgbl-1-2014-s1306", dict(article="1", nummer="6"), "2014-08-05",
     COMPUTED),
    ("bgbl-1-2014-s1306", dict(article="1", nummer="3"), "2015-01-01",
     OFFICIAL),
    # AufenthG 2020 (BGBl. I S. 166).
    ("bgbl-1-2020-s166", dict(article="1", nummer="26a"), "2020-02-20",
     COMPUTED),
    ("bgbl-1-2020-s166", dict(article="4b", nummer="1"), "2020-03-01",
     OFFICIAL),
    ("bgbl-1-2020-s166", dict(article="4b", nummer="3"), "2020-02-20",
     COMPUTED),
    ("bgbl-1-2020-s166", dict(article="2"), "2020-09-01", OFFICIAL),
    # EMöGG 2017 (BGBl. I S. 3546): "sechs Kalendermonate nach der
    # Verkündung" -- period rule §§ 187 Abs. 1, 188 Abs. 2 BGB.
    ("bgbl-1-2017-s3546", dict(article="1", nummer="1"), "2018-04-19",
     COMPUTED),
    ("bgbl-1-2017-s3546", dict(article="5"), "2017-10-19", COMPUTED),
    # BImSchG 2026 (BGBl. 2026 I Nr. 163): "am zweiten Tag nach der
    # Verkündung"; "In Artikel 1 tritt Nummer 10 und in Nummer 14 tritt der
    # § 37m des BImSchG am Tag nach der Verkündung in Kraft."
    ("bgbl-1-2026-163", dict(article="1", nummer="10"), "2026-06-06",
     COMPUTED),
    ("bgbl-1-2026-163", dict(article="1", nummer="1"), "2026-06-07",
     COMPUTED),
    ("bgbl-1-2026-163", dict(article="1", nummer="14"), None,
     "unknown:item_split_across_rules"),
    # BNotO 2021 (BGBl. I S. 3338): "In Artikel 3 Nummer 6 treten § 78p …".
    ("bgbl-1-2021-s3338", dict(article="3", nummer="6"), None,
     "unknown:item_split_across_rules"),
    ("bgbl-1-2021-s3338", dict(article="3", nummer="5"), "2022-08-01",
     OFFICIAL),
    # 2005 (BGBl. I S. 818): kerned "Artikel 1 Nr . 3, 11,16,18 und 19".
    ("bgbl-1-2005-s818", dict(article="1", nummer="3"), "2005-04-01",
     COMPUTED),
    ("bgbl-1-2005-s818", dict(article="1", nummer="1"), "2005-03-30",
     COMPUTED),
    ("bgbl-1-2005-s818", dict(article="8", nummer="3", buchstabe="a"),
     "2004-07-01", OFFICIAL),
    ("bgbl-1-2005-s818", dict(article="8", nummer="3", buchstabe="c"),
     "2005-03-30", COMPUTED),
]


@pytest.mark.parametrize("document_id,item,date,status", CASES,
                         ids=[f"{c[0]}:{'/'.join(f'{k}={v}' for k, v in c[1].items())}"
                              for c in CASES])
def test_real_commencement_clauses(document_id, item, date, status):
    result = resolve(document_id, **item)
    if status.startswith("unknown:"):
        assert result["date"] is None
        assert result["status"] == "unknown"
        assert result["reason"] == status.split(":", 1)[1]
    else:
        assert (result["date"], result["status"]) == (date, status)
        assert result["clause"] and result["basis"]


def test_fixture_set_covers_required_documents():
    documents = set(FIXTURES)
    assert {"bgbl-1-2022-s2328", "bgbl-1-2023-217", "bgbl-1-2023-408",
            "bgbl-1-2025-363", "bgbl-1-2026-107"} <= documents
    articles = [row for row in FIXTURES.values()
                if row.get("kind") != "verordnung_clause"
                and "2006" <= row["document_id"][7:11] <= "2026"]
    assert len(articles) >= 30


def test_clause_is_verbatim_and_offsets_point_into_the_source():
    row = FIXTURES["bgbl-1-2022-s2328"]
    result = resolve("bgbl-1-2022-s2328", article="1", nummer="3a")
    evidence = result["evidence"][0]
    # Offsets are relative to the fixture slice, which starts at row start.
    assert row["text"][evidence["start"]:evidence["end"]] == result["clause"]
    assert "Num -\nmer 3a" in result["clause"]   # printed hyphenation kept
    assert result["clause"].startswith("Artikel 1 Nummer 2 Buchstabe a bis c")


def test_retroactive_flag_follows_date_and_phrase():
    retro = resolve("bgbl-1-2021-s5162", article="16")
    assert retro["retroactive"] and retro["retroactive_phrase"]
    late = resolve("bgbl-1-2007-s2326", article="1", nummer="1")
    # "am 1. Oktober 2007" printed in an issue of 15. Oktober 2007.
    assert late["retroactive"] and not late["retroactive_phrase"]
    normal = resolve("bgbl-1-2026-107", article="1a")
    assert not normal["retroactive"]


def test_frühestens_qualifier_takes_the_later_date():
    result = resolve("bgbl-1-2023-217", article="1", nummer="1")
    assert result["date"] == "2023-11-18"
    assert result["timing"]["type"] == "day_after"
    assert result["timing"]["earliest"] == "2023-11-18"
    assert "frühestens" in result["basis"]


def test_months_after_promulgation_records_rule_and_alternative():
    result = resolve("bgbl-1-2017-s3546", article="3")
    assert result["date"] == "2018-04-19"
    assert result["computation_rule"] == "bgb_187_188_period"
    assert result["alternative_reading"]["date"] == "2018-04-18"


def test_target_norm_decides_amended_law_scopes():
    # Only § 707d of Nummer 3 was brought forward: a leaf that changes
    # § 707d follows that rule, another § of Nummer 3 the default.
    inside = resolve("bgbl-1-2021-s3436", target={"para": "707d"},
                     article="1", nummer="3")
    assert inside["date"] == "2021-08-18"
    other = resolve("bgbl-1-2021-s3436", target={"para": "707c"},
                    article="1", nummer="3")
    assert other["date"] == "2024-01-01"
    zpo = resolve("bgbl-1-2018-s1151", target={"para": "609", "absatz": "7"},
                  article="2", nummer="5")
    assert zpo["date"] == "2018-07-18"
    bimschg = resolve("bgbl-1-2026-163", target={"para": "37m"},
                      article="1", nummer="14")
    assert bimschg["date"] == "2026-06-06"


def test_level_ambiguity_is_recorded_and_resolved_by_structure():
    # "Artikel 3 Absatz 1, 3, 6 bis 8, 9 Nummer 1 und 14": Absatz 9 (AsylbLG)
    # of that act has Nummern 1 and 2 only, and there is an Absatz 14.
    document = parsed("bgbl-1-2016-s1824")
    rule = next(r for r in document["rules"] if r["paragraph"] == "2")
    assert rule["scope"]["level_ambiguities"] == [{
        "literal": [["article", "3"], ["absatz", "9"], ["nummer", "14"]],
        "alternative": [["article", "3"], ["absatz", "14"]]}]
    structure = {"3": {"nummern": {"1", "2"}, "complete": False,
                       "absaetze": {str(n): ({"1", "2"} if n == 9 else set(),
                                              n == 9) for n in range(1, 16)}}}
    disambiguate_levels(document, structure)
    query = item_path("3", absatz="14")
    assert resolve_in_force(document, query,
                            promulgated="2016-07-29")["date"] == "2017-01-01"


def test_literal_reading_stands_when_structure_cannot_disprove_it():
    document = parsed("bgbl-1-2022-s2328")
    structure = {"12": {"nummern": set(), "complete": False,
                        "absaetze": {str(n): (set(), False)
                                     for n in range(1, 25)}}}
    disambiguate_levels(document, structure)
    result = resolve_in_force(
        document, item_path("12", absatz="14", nummer="1", buchstabe="a",
                            doppelbuchstabe="aa", dreifachbuchstabe="ccc"),
        promulgated="2022-12-20")
    assert result["date"] == "2023-07-01"


def test_verordnung_clauses():
    quoted = FIXTURES["bgbl-1-2021-s1174"]["text"]
    body = quoted[quoted.index("Diese Verordnung"):quoted.index("“")]
    rules = parse_commencement_text(body)["rules"]
    assert [(r["event"], r["kind"], r["timing"]["type"]) for r in rules][0] \
        == ("in_kraft", "default", "day_after")
    assert rules[1]["event"] == "ausser_kraft"
    assert rules[1]["timing"]["date"] == "2022-05-31"
    standalone = FIXTURES["bgbl-1-2000-s1590"]["text"]
    parsed_vo = parse_commencement_text(standalone[:standalone.index("\n")])
    result = resolve_in_force(parsed_vo, item_path("1"))
    assert (result["date"], result["status"]) == ("2001-01-01", OFFICIAL)


def test_old_spelling_and_kerning_are_normalised():
    rules = parsed("bgbl-1-2016-s390")["rules"]
    assert rules[0]["timing"]["type"] == "day_after"
    assert "T ag" in rules[0]["clause"]            # verbatim keeps kerning


def test_außerkrafttreten_and_citations_are_not_scopes():
    document = parsed("bgbl-1-2022-s959")
    ausser = [r for r in document["rules"] if r["event"] == "ausser_kraft"]
    assert ausser and all(rule["scope"]["paths"] == [] for rule in ausser)
    # "die zuletzt durch Artikel 187 der Verordnung … geändert worden ist"
    assert all(["article", "187"] not in path for rule in document["rules"]
               for path in rule["scope"]["paths"])


def test_scope_grammar_on_real_scope_expressions():
    def paths(text):
        return [[(level, value.as_json()) for level, value in path]
                for path in parse_scope(text).paths]
    # BGBl. 2023 I Nr. 408, Art. 21 Abs. 4: "13a Nummer 2" is Artikel 13a.
    assert paths("Artikel 3 Nummer 6, die Artikel 12, 13 Nummer 4, 13a "
                 "Nummer 2, die Artikel 14, 15 und 20") == [
        [("article", "3"), ("nummer", "6")], [("article", "12")],
        [("article", "13"), ("nummer", "4")],
        [("article", "13a"), ("nummer", "2")], [("article", "14")],
        [("article", "15")], [("article", "20")]]
    # BGBl. I 2022 S. 2328, Art. 13 Abs. 2 (excerpt).
    assert paths("Artikel 1 Nummer 2 Buchstabe a bis c, Nummer 3a") == [
        [("article", "1"), ("nummer", "2"),
         ("buchstabe", {"from": "a", "to": "c", "to_exclusive": False})],
        [("article", "1"), ("nummer", "3a")]]
    # A deeper keyword narrows only the last value of a range.
    assert paths("Artikel 2 Nr. 1 bis 3 und Nr. 9 bis 13 Buchstabe a") == [
        [("article", "2"), ("nummer", {"from": "1", "to": "3",
                                       "to_exclusive": False})],
        [("article", "2"), ("nummer", {"from": "9", "to": "13",
                                       "to_exclusive": True})],
        [("article", "2"), ("nummer", "13"), ("buchstabe", "a")]]
    excluded = parse_scope("Artikel 1 mit Ausnahme von Nummer 18 Buchstabe b "
                           "und der Nummern 30 und 31")
    assert [[(l, v.as_json()) for l, v in p] for p in excluded.exclusions] == [
        [("article", "1"), ("nummer", "18"), ("buchstabe", "b")],
        [("article", "1"), ("nummer", "30")],
        [("article", "1"), ("nummer", "31")]]
    # BGBl. I 2011 S. 453: an exclusion inside Nummer 10 ends at "Nummer 11".
    mixed = parse_scope("Artikel 2 Nummer 1 bis 6, 7a, 8, 9, 10 mit Ausnahme "
                        "von Buchstabe c Doppelbuchstabe cc, Nummer 11, 12")
    assert [("article", "2"), ("nummer", "11")] in [
        [(l, v.as_json()) for l, v in p] for p in mixed.paths]


def test_inserted_items_fall_inside_ranges():
    # "Nummer 7 bis 11" includes an inserted Nummer 8a.
    result = resolve("bgbl-1-2022-s2328", article="1", nummer="8a")
    assert result["date"] == "2023-07-01"


@pytest.mark.parametrize("timing,promulgated,expected", [
    ({"type": "first_day_of_month", "n": 1}, "2016-07-29", "2016-08-01"),
    ({"type": "first_day_of_month", "n": 18}, "2011-12-29", "2013-06-01"),
    ({"type": "first_day_of_month", "n": 1}, "2022-12-20", "2023-01-01"),
    ({"type": "first_day_of_quarter", "n": 1}, "2022-12-28", "2023-01-01"),
    ({"type": "first_day_of_quarter", "n": 1}, "2024-05-15", "2024-07-01"),
    ({"type": "first_day_of_year", "n": 1}, "2024-05-15", "2025-01-01"),
    ({"type": "nth_day_after", "n": 20}, "2013-04-12", "2013-05-02"),
    ({"type": "months_after", "n": 6}, "2019-08-31", "2020-03-01"),
    ({"type": "months_after", "n": 3}, "2024-03-26", "2024-06-27"),
    ({"type": "day_after", "earliest": "2023-11-18"}, "2023-08-18",
     "2023-11-18"),
])
def test_relative_timings(timing, promulgated, expected):
    assert compute_timing(timing, promulgated)[0] == expected


def test_relative_timing_without_promulgation_stays_unknown():
    assert compute_timing({"type": "day_after"}, None) == (
        None, None, "promulgation_date_unknown")
    result = resolve_in_force(parsed("bgbl-1-2025-363"), item_path("1"))
    assert result["status"] == "unknown"
    assert result["reason"] == "promulgation_date_unknown"


def test_dip_cross_check_findings():
    row = FIXTURES["bgbl-1-2023-217"]
    query = item_path("1", nummer="18", buchstabe="b")
    printed = resolve("bgbl-1-2023-217", article="1", nummer="18",
                      buchstabe="b")
    assert dip_cross_check(row["dip_entry_into_force"], query,
                           printed)["status"] == "agree"
    # DIP writes "Artikel 4 Nr. 1a" for "Artikel 4 Nummer 1 Buchstabe a".
    row = FIXTURES["bgbl-1-2022-s760"]
    query = item_path("4", nummer="1", buchstabe="a")
    finding = dip_cross_check(row["dip_entry_into_force"], query,
                              resolve("bgbl-1-2022-s760", article="4",
                                      nummer="1", buchstabe="a"))
    assert finding["status"] == "agree"
    assert finding["dip_reading"] == "dip_compressed_item_notation"
    # DIP elides its own list ("...") for the Bürgergeld-Gesetz.
    row = FIXTURES["bgbl-1-2022-s2328"]
    query = item_path("12", absatz="14", nummer="1", buchstabe="a",
                      doppelbuchstabe="aa", dreifachbuchstabe="ccc")
    finding = dip_cross_check(row["dip_entry_into_force"], query, resolve(
        "bgbl-1-2022-s2328", article="12", absatz="14", nummer="1",
        buchstabe="a", doppelbuchstabe="aa", dreifachbuchstabe="ccc"))
    assert finding["status"] == "dip_inconclusive_truncated"
    # DIP records one row (28.12.2012); the print says 1. September 2013.
    row = FIXTURES["bgbl-1-2012-s2745"]
    finding = dip_cross_check(row["dip_entry_into_force"],
                              item_path("1", nummer="1"),
                              resolve("bgbl-1-2012-s2745", article="1",
                                      nummer="1"))
    assert finding["status"] == "disagree"
    assert (finding["dip_date"], finding["printed_date"]) == (
        "2012-12-28", "2013-09-01")


def test_applies_from_is_not_stated_without_an_application_clause():
    document = parsed("bgbl-1-2023-217")
    assert resolve_applies_from(document, item_path("1", nummer="5")) == {
        "date": None, "status": "not_stated", "basis": None}
