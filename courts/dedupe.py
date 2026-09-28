"""Offline repair of duplicates and ECLI collisions already stored under older rules.

Two cases, both inside one source (cross-source twins: courts.sync.reexamine_twins):

* republications - one decision published twice under two document ids (NRW under a second URL spelling or
  year folder, juris portals under a re-issued document number).  The store now refuses the second copy
  (courts.store.republication_of); here the stored pairs are merged the same way: one copy stays, the other
  becomes a skipped duplicate_identity item whose capture remains in the packs (an offline verification pair).
* shared ECLIs - several different documents stored with one ECLI.  The document whose date and Az the
  ordinal encodes owns it (courts.store.ecli_fit); the others lose it to metadata.ecli_published_other_document
  and, if one of them held the ECLI as its id, move to their synthetic id.  Ties stay ambiguous.

Every action is returned as a journal entry; nothing is written without apply=True.
"""
from __future__ import annotations

import collections
import json
import sqlite3

from .casenumbers import case_key
from .store import (REPUBLICATION_MIN_OVERLAP, _delete_decision, _loose_ecli, ecli_fit, named_by_corrections,
                    paragraph_overlap, rekey_decision, release_ecli)


def _texts(conn: sqlite3.Connection, decision_id: str) -> list[str]:
    return [t for (t,) in conn.execute("SELECT text FROM paragraphs WHERE decision_id=? ORDER BY ordinal",
                                       (decision_id,))]


def _keep_order(row: sqlite3.Row) -> tuple:
    # Explicit Randnummern first, then the fuller text, then the copy stored first.
    return (row["numbering"] != "rn", -row["paragraph_count"], row["first_seen"], row["source_doc_id"])


def _compatible_eclis(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return True
    la, lb = _loose_ecli(a), _loose_ecli(b)
    return la.startswith(lb) or lb.startswith(la)


def republication_clusters(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, list[tuple[sqlite3.Row, float]]]]:
    """[(keeper, [(duplicate, overlap), ...]), ...] for every stored republication."""
    rows = conn.execute("""SELECT d.id, d.ecli, d.source_id, d.source_doc_id, d.court_slug, d.decision_date,
                                  d.decision_type, d.paragraph_count, d.numbering, d.first_seen, c.case_key
                           FROM decisions d JOIN case_numbers c ON c.decision_id = d.id AND c.position = 1
                           WHERE (d.source_id, d.court_slug, d.decision_date, c.case_key) IN (
                               SELECT d2.source_id, d2.court_slug, d2.decision_date, c2.case_key
                               FROM decisions d2 JOIN case_numbers c2 ON c2.decision_id = d2.id AND c2.position = 1
                               GROUP BY 1, 2, 3, 4 HAVING count(*) > 1)""").fetchall()
    groups: dict[tuple, list[sqlite3.Row]] = collections.defaultdict(list)
    for row in rows:
        kind = (row["decision_type"] or "").split(" ")[0].lower()
        groups[(row["source_id"], row["court_slug"], row["decision_date"], row["case_key"], kind)].append(row)
    clusters = []
    for members in groups.values():
        if len(members) < 2:
            continue
        texts = {m["id"]: _texts(conn, m["id"]) for m in members}
        parent = {m["id"]: m["id"] for m in members}
        best: dict[str, float] = {}

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if not _compatible_eclis(a["ecli"], b["ecli"]):
                    continue
                overlap = paragraph_overlap(texts[a["id"]], texts[b["id"]])
                if overlap >= REPUBLICATION_MIN_OVERLAP:
                    parent[find(a["id"])] = find(b["id"])
                    best[a["id"]] = max(best.get(a["id"], 0), overlap)
                    best[b["id"]] = max(best.get(b["id"], 0), overlap)
        by_root: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
        for m in members:
            by_root[find(m["id"])].append(m)
        for cluster in by_root.values():
            if len(cluster) < 2:
                continue
            cluster.sort(key=_keep_order)
            clusters.append((cluster[0], [(m, best.get(m["id"], 0.0)) for m in cluster[1:]]))
    return clusters


def merge_republications(conn: sqlite3.Connection, *, apply: bool = False) -> list[dict]:
    journal = []
    for keeper, duplicates in republication_clusters(conn):
        for dup, overlap in duplicates:
            entry = {"action": "merge_republication", "keep": keeper["id"],
                     "keep_item": f"{keeper['source_id']}:{keeper['source_doc_id']}", "drop": dup["id"],
                     "drop_item": f"{dup['source_id']}:{dup['source_doc_id']}", "overlap": round(overlap, 3),
                     "drop_paragraphs": dup["paragraph_count"], "keep_paragraphs": keeper["paragraph_count"]}
            protected = conn.execute("SELECT count(*) FROM relation_assessments WHERE decision_id=? "
                                     "AND derived_by<>'rules'", (dup["id"],)).fetchone()[0]
            if named_by_corrections(conn, dup["id"]) or protected:
                entry["status"] = "kept: named by corrections or reviewed assessments"
                journal.append(entry)
                continue
            if apply:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute("UPDATE relations SET target_decision_id=? WHERE target_decision_id=?",
                                 (keeper["id"], dup["id"]))
                    _delete_decision(conn, dup["id"])
                    conn.execute("""UPDATE source_items SET state='skipped', decision_id=NULL,
                                    failure_kind='duplicate_identity', failure_detail=?
                                    WHERE source_id=? AND source_doc_id=?""",
                                 (f"{keeper['id']} already stored from {keeper['source_id']}:{keeper['source_doc_id']} "
                                  f"(republication: {overlap:.0%} of the paragraphs identical)",
                                  dup["source_id"], dup["source_doc_id"]))
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            entry["status"] = "merged" if apply else "would merge"
            journal.append(entry)
    return journal


def settle_shared_eclis(conn: sqlite3.Connection, *, apply: bool = False) -> list[dict]:
    journal = []
    eclis = [r[0] for r in conn.execute("SELECT ecli FROM decisions WHERE ecli IS NOT NULL AND ecli<>'' "
                                        "GROUP BY ecli HAVING count(*) > 1")]
    for ecli in eclis:
        rows = conn.execute("SELECT id, source_id, source_doc_id, decision_date, case_numbers FROM decisions "
                            "WHERE ecli=?", (ecli,)).fetchall()
        fits = {r["id"]: ecli_fit(ecli, r["decision_date"], json.loads(r["case_numbers"])) for r in rows}
        top = max(fits.values())
        owners = [r for r in rows if fits[r["id"]] == top]
        entry = {"action": "settle_ecli", "ecli": ecli,
                 "documents": [{"id": r["id"], "item": f"{r['source_id']}:{r['source_doc_id']}",
                                "date": r["decision_date"], "fit": fits[r["id"]]} for r in rows]}
        if len(owners) != 1:
            entry["status"] = "kept ambiguous: several documents fit the ordinal alike"
            journal.append(entry)
            continue
        owner = owners[0]
        entry["owner"] = owner["id"]
        losers = [r for r in rows if r["id"] != owner["id"]]
        if any(named_by_corrections(conn, r["id"]) for r in rows):
            entry["status"] = "kept: named by corrections"
            journal.append(entry)
            continue
        if apply:
            conn.execute("BEGIN IMMEDIATE")
            try:
                moved = {}
                for loser in losers:
                    moved[loser["id"]] = release_ecli(
                        conn, loser["id"], ecli,
                        f"its date/Az fit the ordinal less than {owner['source_id']}:{owner['source_doc_id']}")
                if owner["id"] != ecli and not conn.execute("SELECT 1 FROM decisions WHERE id=?", (ecli,)).fetchone():
                    rekey_decision(conn, owner["id"], ecli)
                    moved[owner["id"]] = ecli
                    meta = json.loads(conn.execute("SELECT metadata FROM decisions WHERE id=?", (ecli,)).fetchone()[0]
                                      or "{}")
                    meta.pop("ecli_shared_with", None)
                    meta.pop("id_variant", None)
                    conn.execute("UPDATE decisions SET metadata=? WHERE id=?",
                                 (json.dumps(meta, ensure_ascii=False, sort_keys=True), ecli))
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            entry["moved"] = {k: v for k, v in moved.items() if v and v != k}
        entry["status"] = "settled" if apply else "would settle"
        journal.append(entry)
    return journal


def dedupe(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Republications first (identical copies must not compete for an ECLI), then shared ECLIs."""
    merged = merge_republications(conn, apply=apply)
    settled = settle_shared_eclis(conn, apply=apply)
    return {"republications": merged, "shared_eclis": settled,
            "summary": {"republications": collections.Counter(e["status"] for e in merged),
                        "shared_eclis": collections.Counter(e["status"] for e in settled)}}
