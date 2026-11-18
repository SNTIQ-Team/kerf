"""Generate a bounded neutral metadata input, or attach it to a prepared web release."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import sys
from unicodedata import normalize
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from api.court_observations import FILENAME, PROFILE, MAX_BYTES, MAX_ROWS, identity_keys, validate_metadata
from api.public_observations import observation


def encoded(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True,separators=(",",":"))+"\n").encode()


def atomic(path, data):
    with NamedTemporaryFile(dir=path.parent,delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.flush()
            os.fchmod(f.fileno(),0o644)
            os.fsync(f.fileno())
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    try:
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)


def source_rows(scope):
    from courts.read import open_db
    from courts.store import read_capture
    from courts.sources import get_adapter
    from courts.sources.base import Capture
    from courts.sources.rii import COURT_KEYS

    profiles = {"rii":"rii-court-metadata/1","he":"hessen-court-metadata/1","nrw":"nrwe-court-metadata/1"}
    bavarian_pdfs = {"lag":"by-arbg-pdf-metadata/1", "vgh_press":"by-vgh-pdf-metadata/1", "verfgh":"by-verfgh-pdf-metadata/1"}
    supported = set(profiles) | {"by", "by-recht"}
    federal = {v:k for k,v in COURT_KEYS.items()}
    adapters = {s:get_adapter(s) for s in supported}
    rows,excluded = [], []
    if not scope or len(scope) > MAX_ROWS or len(set(scope)) != len(scope):
        raise ValueError("expected distinct selected decision IDs, at most 10000")
    with closing(open_db()) as db:
        for key in scope:
            d = db.execute("SELECT * FROM decisions WHERE id=?",(key,)).fetchone()
            if d is None or d["source_id"] not in supported:
                raise ValueError(f"unknown or unsupported court decision: {key}")
            date_notes = db.execute("SELECT kind,new FROM corrections WHERE target=? AND field='decision_date'",(key,))
            if any(r["kind"]=="decision_field" or (r["kind"]=="other" and isinstance(n:=json.loads(r["new"]),dict) and n.get("status")=="unresolved") for r in date_notes):
                excluded.append({"id":key,"reason":"decision_date_requires_review"})
                continue
            item=db.execute("SELECT i.*,c.extension FROM source_items i JOIN captures c ON c.sha256=i.content_hash WHERE i.source_id=? AND i.source_doc_id=? AND i.state='stored' AND i.decision_id=? AND i.content_hash=?",
                            (d["source_id"],d["source_doc_id"],key,d["content_hash"])).fetchone()
            if item is None:
                raise ValueError(f"missing exact capture provenance: {key}")
            body=read_capture(db,d["content_hash"],d["capture_path"])
            s=d["source_id"]
            # For LAG records the listing URL is an HTML page linking the exact PDF.
            url = d["source_url"] if s == "by" else item["url"]
            parsed=adapters[s].parse(Capture(body,url,d["media_type"],item["extension"]),item)
            if s in {"by", "by-recht"}:
                register = db.execute("SELECT land_code FROM amtsgraph_courts WHERE court_id=?", (d["court_id"],)).fetchone()
                if d["land"] != "by" or parsed.land != "by" or not register or register["land_code"] != "09":
                    excluded.append({"id":key,"reason":"bavarian_court_not_verified_in_register"})
                    continue
            if parsed.metadata.get("listed_date_conflict"):
                excluded.append({"id":key,"reason":"official_list_and_document_dates_disagree"})
                continue
            if (parsed.decision_date,parsed.case_numbers_raw,parsed.court_name,parsed.decision_type)!=(d["decision_date"],d["case_numbers_raw"],d["court_name"],d["decision_type"]):
                excluded.append({"id":key,"reason":"parsed_identity_disagreement"})
                continue
            doc=d["source_doc_id"]
            ecli=d["ecli"] if parsed.ecli and parsed.ecli.casefold()==(d["ecli"] or "").casefold() else None
            profile = profiles.get(s)
            if s=="by":
                profile = bavarian_pdfs[json.loads(item["extra"])["route"]]
                identity = "by-pdf-" + hashlib.sha256(doc.encode()).hexdigest()
            elif s=="by-recht":
                profile = "bayern-recht-metadata/1"
                identity = "by-recht-" + doc.lower()
            elif s=="rii":
                identity="rii-"+doc.removeprefix("jb-").lower()
            elif s=="he":
                identity="he-"+doc.lower()
            else:
                identity="nrw-"+hashlib.sha256(doc.encode()).hexdigest()
            row=dict(id=identity,reference=d["case_numbers_raw"],court=d["court_name"],court_short=federal.get(d["court_slug"],d["court_name"]),
                     event_date=d["decision_date"],document_type=d["decision_type"],ecli=ecli,published_at=None,norm_mentions=[],
                     source=dict(profile=profile,url=url,media_type=d["media_type"],content_sha256=d["content_hash"],
                                 observed_at=d["observed_at"],lineage_key=ecli or key,parser_version=d["parser_version"],canonical_decision_id=key))
            validate_metadata(row)
            rows.append(row)
    return {"profile":PROFILE,"observations":sorted(rows,key=lambda r:r["id"]),"excluded":excluded}


def attach(source, destination):
    from api.decision_passages import database
    source=Path(source)
    if not source.is_file():
        return
    if source.stat().st_size > MAX_BYTES:
        raise ValueError("oversize court observation input")
    data=json.loads(source.read_bytes())
    if data["profile"]!=PROFILE or len(data["observations"])>MAX_ROWS:
        raise ValueError("unsupported court observation input")
    root=Path(destination).resolve()
    conn, manifest=database(root)
    with closing(conn):
        base=[observation(conn,r) for r in conn.execute("SELECT * FROM decisions WHERE status='indexed' ORDER BY id")]
    # A legacy source may spell the same public identity differently or omit its ECLI.
    # Keep two distinct published ECLIs even if date, court, docket and type coincide.
    def docket_identities(row):
        def normal(value):
            return re.sub(r"[^\w]", "", normalize("NFKC",value or "").casefold())
        return {(normal(court),normal(row["reference"]),row["event_date"],normal(row["document_type"]))
                for court in (row["court"],row["court_short"])}
    docket_ids={}
    keys={k for row in base for k in identity_keys(row)}
    rows=list(base)
    for row in base:
        for docket in docket_identities(row):
            docket_ids.setdefault(docket,set()).add((row.get("ecli") or "").casefold())
    for row in data["observations"]:
        validate_metadata(row)
        these=identity_keys(row)
        dockets=docket_identities(row)
        ecli=(row.get("ecli") or "").casefold()
        previous=set().union(*(docket_ids.get(docket,set()) for docket in dockets))
        same_docket=bool(previous) and (not ecli or "" in previous or ecli in previous)
        if keys.isdisjoint(these) and not same_docket:
            rows.append(row)
            for docket in dockets:
                docket_ids.setdefault(docket,set()).add(ecli)
        keys.update(these)
    if len(rows)>MAX_ROWS:
        raise ValueError("public observation consumer limit exceeded")
    payload=encoded(dict(profile=PROFILE,passage_database_sha256=manifest["database_sha256"],observations=sorted(rows,key=lambda r:r["id"])))
    if len(payload)>MAX_BYTES:
        raise ValueError("oversize observation projection")
    digest=hashlib.sha256(payload).hexdigest()
    atomic(root/FILENAME,payload)
    descriptor=dict(sha256=digest,count=len(rows),metadata_only=len(rows)-len(base))
    atomic(root/"decision_passages.json",encoded(dict(manifest,public_observations=descriptor)))
    return descriptor


def merge_inputs(previous, addition):
    if previous["profile"] != PROFILE or addition["profile"] != PROFILE:
        raise ValueError("unsupported observation input")
    rows = {}
    for row in [*previous["observations"], *addition["observations"]]:
        validate_metadata(row)
        saved = rows.setdefault(row["id"], row)
        if saved != row:
            raise ValueError(f"conflicting observations for {row['id']}")
    result = {"profile": PROFILE, "observations": sorted(rows.values(), key=lambda r:r["id"]),
              "excluded": list(previous.get("excluded", []))}
    for item in addition.get("excluded", []):
        if item not in result["excluded"]:
            result["excluded"].append(item)
    if len(rows) > MAX_ROWS or len(encoded(result)) > MAX_BYTES:
        raise ValueError("oversize court metadata input")
    return result


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest="op",required=True)
    exp=sub.add_parser("export");exp.add_argument("--intake",type=Path,required=True);exp.add_argument("--output",type=Path,required=True)
    exp.add_argument("--previous",type=Path,help="retain observations and exclusions from this durable input")
    stage=sub.add_parser("stage");stage.add_argument("--source",type=Path,required=True);stage.add_argument("--destination",type=Path,required=True)
    a=cli.parse_args()
    if a.op=="export":
        scope=[i for ids in json.loads(a.intake.read_bytes())["added_ids"].values() for i in ids]
        data=source_rows(scope)
        if a.previous:
            data = merge_inputs(json.loads(a.previous.read_bytes()), data)
        atomic(a.output,encoded(data))
        print(json.dumps({"observations":len(data["observations"]),"excluded":data["excluded"]}))
    else:
        if not a.source.is_file(): cli.error("source file missing")
        print(json.dumps(attach(a.source,a.destination)))


if __name__=="__main__":
    main()
