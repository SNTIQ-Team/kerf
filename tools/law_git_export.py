"""Export Lexgraph's canonical norm versions as a real Git repository.

Git is an *export view*; Lexgraph stays canonical.  The repository has one
Markdown file per norm (front matter: identity, version id, legal validity,
source role, content hashes, amending act) and one commit per consolidated
version, in chronological order:

* author date   = ``valid_from`` when it is verified; otherwise the first
                  official retrieval observation, and the commit says so in
                  its ``Lexgraph-Author-Date-Basis`` trailer;
* message       = the amending act, its BGBl/GVBl reference and the
                  commencement basis (or an explicit "unresolved");
* file bodies   = one legal sentence per line, so ``git blame`` works per
                  sentence and ``git diff`` aligns with Absatz/Satz/Nummer.

The export is incremental and idempotent: every commit carries a
deterministic ``Lexgraph-Commit-Fingerprint``; a rerun appends only commits
beyond the longest unchanged prefix.  If Lexgraph's knowledge changed an
earlier commit (e.g. a legal start was verified later), history is rebuilt
from that point and the previous head is kept under
``refs/lexgraph/superseded/<n>``.

Usage::

    python3 tools/law_git_export.py --data web/data \
        --repo ~/.local/share/sntiq-legal/law-git [--acts fed_sgb_14,by_agsg]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.evidence import EnvelopeError  # noqa: E402
from api.law_structure import (  # noqa: E402
    STRUCTURE_PARSER_VERSION,
    normalize_sentence,
    parse_norm_structure,
)
from api.norm_history import (  # noqa: E402
    ActChain,
    NormHistoryStore,
    NormVersion,
    StateVersion,
    norm_content_hash,
)
from api.norm_identity import gii_norm_url, load_gii_toc, unit_label  # noqa: E402
from api.retrospective_store import validate_manifest  # noqa: E402

EXPORTER = "lexgraph-law-git/1"
AUTHOR = "Lexgraph Export <law-git@sntiq.com>"
DEFAULT_REPO = Path.home() / ".local" / "share" / "sntiq-legal" / "law-git"
BRANCH = "refs/heads/main"


# ---------------------------------------------------------------- rendering

def _yaml(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return json.dumps(str(value), ensure_ascii=False)


def render_body(text: str, *, bavarian: bool) -> list[str]:
    """One legal sentence per line; Absätze separated by a blank line."""
    structure = parse_norm_structure(text, bavarian=bavarian)
    lines: list[str] = []
    for absatz in structure["units"]:
        if lines:
            lines.append("")
        marker = absatz.get("marker")
        first = True
        for satz in absatz.get("children") or []:
            prefix = f"{marker} " if (marker and first) else ""
            first = False
            children = satz.get("children") or []
            if not children:
                lines.append(prefix + normalize_sentence(satz["text"]))
                continue
            for child in children:
                if child["type"] == "text_block":
                    words = normalize_sentence(child["text"])
                    if child["number"] == "einleitung":
                        lines.append(prefix + words)
                        prefix = ""
                    else:
                        lines.append(words)
                    continue
                if prefix:
                    lines.append(prefix.rstrip())
                    prefix = ""
                lines.append(f"{child.get('marker') or child['number'] + '.'} "
                             + normalize_sentence(_own_text(child)))
                for letter in child.get("children") or []:
                    lines.append(f"   {letter.get('marker')} "
                                 + normalize_sentence(_own_text(letter)))
                    for double in letter.get("children") or []:
                        lines.append(f"      {double.get('marker')} "
                                     + normalize_sentence(double["text"]))
        if first and marker:
            lines.append(marker)
    if not lines and text.strip():
        lines.append(normalize_sentence(text))
    return lines


def _own_text(node: dict[str, Any]) -> str:
    """Text of a list item without the text of its nested sub-items."""
    children = node.get("children") or []
    if not children:
        return node["text"]
    offset = node["start"] + len(node.get("marker") or "")
    cut = children[0]["start"]
    whole = node["text"]
    head = whole[:max(0, cut - offset)]
    return head if head.strip() else whole


def render_norm(chain: ActChain, version: NormVersion,
                toc: dict[str, Any] | None) -> str:
    state = version.first_state
    unit, number = version.norm_id.split("/")[-2:]
    number = number.split("~")[0]
    slug = chain.prefix.split("/")[2]
    if chain.bavarian:
        url = {"url": None, "status": "not_resolved",
               "act_url": chain.act.get("permalink")}
        role = state.source_role
    else:
        url = gii_norm_url(toc, slug, unit, number)
        role = ("verified_reconstruction" if state.derived else
                "official_consolidation_gii")
    structure = parse_norm_structure(version.text, bavarian=chain.bavarian)
    amendment = state.amendment or {}
    fields = [
        ("id", version.norm_id),
        ("jurisdiction", "/".join(chain.prefix.split("/")[:2])),
        ("act_id", chain.act.get("id")),
        ("act", chain.act.get("jurabk")),
        ("act_title", chain.act.get("title")),
        ("label", version.label or unit_label(unit, number)),
        ("title", version.title or None),
        ("version_id", version.version_id),
        ("valid_from", state.valid_from),
        ("valid_from_status", state.valid_from_status),
        ("valid_from_basis", state.valid_from_basis),
        ("observed_from", state.first_observed_at),
        ("date_axes", "valid_from is legal validity; observed_from is a "
                      "retrieval observation and never a legal date"),
        ("source_role", role),
        ("source_url", url.get("url") or url.get("act_url")),
        ("source_url_status", url.get("status")),
        ("state_sha256", state.key if len(state.key) == 64 else None),
        ("content_sha256", version.content_sha256),
        ("text_sha256", hashlib.sha256(version.text.encode("utf-8"))
         .hexdigest()),
        ("source_exact", not state.derived),
        ("reconstruction_status", "verified_reconstruction" if state.derived
         else "not_reconstructed"),
        ("amending_act", amendment.get("act")),
        ("amending_act_reference", amendment.get("reference")),
        ("amending_act_basis", amendment.get("basis")),
        ("commencement_basis", amendment.get("commencement_basis")),
        ("legal_effect_verified", bool(amendment.get(
            "legal_effect_verified"))),
        ("structure", {key: value for key, value in
                       structure["counts"].items() if value}),
        ("structure_parser", STRUCTURE_PARSER_VERSION),
        ("rendering", "one legal sentence per line; canonical bytes are "
                      "identified by text_sha256"),
        ("exporter", EXPORTER),
    ]
    lines = ["---", *(f"{key}: {_yaml(value)}" for key, value in fields),
             "---", ""]
    heading = version.label or unit_label(unit, number)
    if version.title:
        heading = f"{heading} {version.title}"
    lines.append(f"# {heading}")
    lines.append("")
    lines.extend(render_body(version.text, bavarian=chain.bavarian))
    return "\n".join(lines).rstrip() + "\n"


def render_act(chain: ActChain, state: StateVersion) -> str:
    amendment = state.amendment or {}
    fields = [
        ("act_id", chain.act.get("id")),
        ("prefix", chain.prefix),
        ("act", chain.act.get("jurabk")),
        ("title", chain.act.get("title")),
        ("consolidated_version", state.key),
        ("valid_from", state.valid_from),
        ("valid_from_status", state.valid_from_status),
        ("observed_from", state.first_observed_at),
        ("source_role", state.source_role if chain.bavarian else (
            "verified_reconstruction" if state.derived else
            "official_consolidation_gii")),
        ("source_url", state.source_url),
        ("source_build", state.builddate),
        ("stand", state.stand),
        ("amending_act", amendment.get("act")),
        ("amending_act_reference", amendment.get("reference")),
        ("commencement_basis", amendment.get("commencement_basis")),
        ("history_completeness", "per-norm archived versions only"
         if chain.bavarian else "complete consolidated states"),
        ("exporter", EXPORTER),
    ]
    body = ["---", *(f"{key}: {_yaml(value)}" for key, value in fields),
            "---", "", f"# {chain.act.get('title') or chain.act.get('id')}",
            ""]
    return "\n".join(body).rstrip() + "\n"


README = """# Lexgraph law history as Git (export view)

This repository is generated by `tools/law_git_export.py` from Lexgraph's
canonical, content-addressed legal data.  **Lexgraph stays canonical**; this
repository is a projection for `git log`, `git diff`, `git blame`.

* One file per norm: `de/bund/<gii-slug>/<para|art|anlage|anhang|text>/<n>.md`
  and `de/by/<act>/<art|…>/<n>.md`.  Front matter holds the identity, version
  id, `valid_from` (+ status and basis), `observed_from`, source role, content
  hashes and the amending act.
* One commit per consolidated version.  The **author date is `valid_from`**
  when Lexgraph verified it (final BGBl command + commencement clause, or the
  BAYERN.RECHT "gültig ab" of an archived official page).  Where the legal
  start is unresolved, the author date is the first official retrieval
  observation and the trailer `Lexgraph-Author-Date-Basis: observed_at`
  says so — that date is **not** a legal date.
* Bodies have one legal sentence per line, so `git blame` answers "which
  version introduced this sentence".  Sentences present in the first stored
  version predate Lexgraph's text history; blame cannot attribute them.
* Federal history starts with the first complete GII state Lexgraph holds
  (plus replay-verified reconstructions).  Bavarian history is per norm from
  archived official pages and is incomplete where no archive exists.
* EU: Lexgraph holds EU instruments as metadata only; there is no versioned
  EU text to export.
* Do not edit this working tree: every export resets it to `main`.
"""


# ---------------------------------------------------------------- planning

@dataclass
class PlannedCommit:
    act_id: str
    state_keys: list[str]
    author_date: str
    author_basis: str
    order_date: str
    subject: str
    body: str
    trailers: list[tuple[str, str]]
    files: dict[str, str | None] = field(default_factory=dict)
    sequence: int = 0

    @property
    def version_key(self) -> str:
        return f"{self.act_id}:{'+'.join(k[:16] for k in self.state_keys)}"

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for part in (self.version_key, self.author_date, self.order_date,
                     self.subject, self.body,
                     json.dumps(self.trailers, ensure_ascii=False)):
            digest.update(part.encode("utf-8") + b"\0")
        for path in sorted(self.files):
            content = self.files[path]
            digest.update(path.encode("utf-8") + b"\0")
            digest.update(b"-" if content is None else
                          hashlib.sha256(content.encode("utf-8")).digest())
        return digest.hexdigest()

    def message(self) -> str:
        trailers = [*self.trailers,
                    ("Lexgraph-Version-Key", self.version_key),
                    ("Lexgraph-Commit-Fingerprint", self.fingerprint())]
        lines = [self.subject, "", self.body.rstrip(), ""]
        lines.extend(f"{key}: {value}" for key, value in trailers)
        return "\n".join(lines) + "\n"


def _tree_at(chain: ActChain) -> list[dict[str, dict[str, Any]]]:
    """Norm rows present in each state (federal exact, Bavarian carried)."""
    trees: list[dict[str, dict[str, Any]]] = [dict() for _ in chain.states]
    for norm_id, rows in chain.norms.items():
        if chain.bavarian:
            rows_sorted = sorted(rows, key=lambda item: item[0])
            for position, (index, row) in enumerate(rows_sorted):
                end = (rows_sorted[position + 1][0]
                       if position + 1 < len(rows_sorted)
                       else len(chain.states))
                for k in range(index, end):
                    trees[k][norm_id] = row
        else:
            for index, row in rows:
                trees[index][norm_id] = row
    return trees


def _path(norm_id: str) -> str:
    return norm_id + ".md"


def plan_act(chain: ActChain, toc: dict[str, Any] | None
             ) -> list[PlannedCommit]:
    trees = _tree_at(chain)
    versions = {norm_id: chain.versions_of(norm_id)
                for norm_id in chain.norms}
    by_start: dict[tuple[str, int], NormVersion] = {}
    for norm_id, items in versions.items():
        for version in items:
            by_start[(norm_id, version.first_state.index)] = version
    commits: list[PlannedCommit] = []
    previous_tree: dict[str, dict[str, Any]] = {}
    last_order = "0000-00-00"
    for state, tree in zip(chain.states, trees):
        files: dict[str, str | None] = {}
        changed_labels: list[str] = []
        for norm_id, row in tree.items():
            old = previous_tree.get(norm_id)
            if old is not None and norm_content_hash(old) == \
                    norm_content_hash(row):
                continue
            version = by_start.get((norm_id, state.index))
            if version is None:
                # Content equal to an earlier non-adjacent version: find the
                # version whose run contains this state.
                version = next(v for v in versions[norm_id]
                               if any(s.index == state.index
                                      for s in v.states))
            files[_path(norm_id)] = render_norm(chain, version, toc)
            changed_labels.append(str(row.get("enbez") or norm_id))
        for norm_id in previous_tree:
            if norm_id not in tree:
                files[_path(norm_id)] = None
                changed_labels.append(f"{previous_tree[norm_id].get('enbez')}"
                                      " (removed)")
        files[f"{chain.prefix}/_act.md"] = render_act(chain, state)
        previous_tree = tree
        if state.valid_from and state.valid_from_status != "unresolved":
            own, basis = state.valid_from, "valid_from"
        elif state.first_observed_at:
            own, basis = state.first_observed_at, "observed_at"
        elif state.valid_until:
            # Only the end is known (an archived page shows the text was
            # current until the next version); never invent its start.
            own, basis = state.valid_until, "superseded_on"
        else:
            own, basis = last_order if last_order != "0000-00-00" else \
                "1970-01-01", "unknown"
        order = max(own, last_order)
        last_order = order
        commits.append(_commit(chain, state, own, basis, order, files,
                               changed_labels))
    if chain.bavarian:
        commits = _merge_same_day(commits)
    return commits


def _commit(chain: ActChain, state: StateVersion, author_date: str,
            basis: str, order: str, files: dict[str, str | None],
            changed: list[str]) -> PlannedCommit:
    act = chain.act.get("jurabk") or chain.act.get("id")
    amendment = state.amendment or {}
    reference = amendment.get("reference")
    if basis == "valid_from":
        what = amendment.get("act") or reference
        if what:
            subject = f"{act}: {what}"
            if reference and reference not in subject:
                subject += f" ({reference})"
        else:
            subject = f"{act}: version (amending act not recorded)"
        subject += f" — in force {state.valid_from}"
    elif basis == "observed_at":
        subject = (f"{act}: consolidated version observed "
                   f"{state.first_observed_at} (legal start unresolved)")
    elif basis == "superseded_on":
        subject = (f"{act}: earlier version, current until "
                   f"{state.valid_until} (legal start unresolved)")
    else:
        subject = f"{act}: version with unknown dates"
    body = [
        f"Amending act: {amendment.get('act') or 'not established'}",
        f"Promulgated in: {reference or 'not established'}"
        + (f" (published {amendment.get('published_at')})"
           if amendment.get("published_at") else ""),
        f"Valid from: {state.valid_from or 'unresolved'} "
        f"({state.valid_from_status}"
        + (f"; basis: {state.valid_from_basis}" if state.valid_from_basis
           else "") + ")",
        f"Commencement basis: {amendment.get('commencement_basis') or 'unresolved'}",
    ]
    if amendment.get("basis") == "gii_stand_note":
        body.append("Note: the amending act is GII's own 'zuletzt geändert "
                    "durch' note for this consolidation; its commencement is "
                    "not verified.")
    if amendment.get("pending_note"):
        body.append(f"GII note: {amendment['pending_note']}")
    if basis == "observed_at":
        body.append("Author date: first official retrieval observation of "
                    "this consolidation — NOT a legal effective date.")
    if basis == "superseded_on":
        body.append("Author date: the day this text stopped being current "
                    "(start of the following archived version); its own "
                    "start is unknown.")
    if state.stand and amendment.get("basis") != "gii_stand_note":
        body.append(f"GII Stand: {' '.join(str(state.stand).split())[:400]}")
    if state.derived:
        body.append("Text: replay-verified reconstruction (source_exact: "
                    "false), not an official capture.")
    shown = changed[:40]
    body.append("")
    body.append(f"Changed norms ({len(changed)}): " + ", ".join(shown)
                + (" …" if len(changed) > len(shown) else ""))
    trailers = [
        ("Lexgraph-Act", str(chain.act.get("id"))),
        ("Lexgraph-State", state.key),
        ("Lexgraph-Valid-From", state.valid_from or "unresolved"),
        ("Lexgraph-Valid-From-Status", state.valid_from_status),
        ("Lexgraph-Observed-From", state.first_observed_at or "unknown"),
        ("Lexgraph-Source-Role", state.source_role if chain.bavarian else (
            "verified_reconstruction" if state.derived else
            "official_consolidation_gii")),
        ("Lexgraph-Author-Date-Basis", basis),
    ]
    if amendment.get("document_id"):
        trailers.append(("Lexgraph-Amending-Document",
                         str(amendment["document_id"])))
    return PlannedCommit(
        act_id=str(chain.act.get("id")), state_keys=[state.key],
        author_date=author_date, author_basis=basis, order_date=order,
        subject=subject, body="\n".join(body), trailers=trailers,
        files=files)


def _merge_same_day(commits: list[PlannedCommit]) -> list[PlannedCommit]:
    """Bavarian per-norm archive states of one date form one version."""
    merged: list[PlannedCommit] = []
    for commit in commits:
        if merged and merged[-1].author_date == commit.author_date and \
                merged[-1].author_basis == commit.author_basis:
            target = merged[-1]
            # Prefer the description that names an amending act.
            if "not recorded" in target.subject and \
                    "not recorded" not in commit.subject:
                target.subject, target.body, target.trailers = \
                    commit.subject, commit.body, commit.trailers
            target.state_keys.extend(commit.state_keys)
            for path, content in commit.files.items():
                target.files[path] = content
            continue
        merged.append(commit)
    return merged


def plan(store: NormHistoryStore, act_ids: Iterable[str],
         toc: dict[str, Any] | None
         ) -> tuple[list[PlannedCommit], list[dict[str, Any]]]:
    commits: list[PlannedCommit] = []
    report: list[dict[str, Any]] = []
    for act_id in act_ids:
        try:
            chain = store.chain(act_id)
        except EnvelopeError as exc:
            report.append({"act_id": act_id, "status": exc.status,
                           "reason": exc.message})
            continue
        act_commits = plan_act(chain, toc)
        for sequence, commit in enumerate(act_commits):
            commit.sequence = sequence
        commits.extend(act_commits)
        report.append({"act_id": act_id, "status": "ok",
                       "versions": len(chain.states),
                       "commits": len(act_commits),
                       "warnings": chain.warnings[:10]})
    # Global chronology; within one act the chain order is authoritative.
    commits.sort(key=lambda c: (c.order_date, c.act_id, c.sequence))
    return commits, report


# --------------------------------------------------------------------- git

def _git(repo: Path, *args: str, input_bytes: bytes | None = None,
         check: bool = True) -> str:
    env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    result = subprocess.run(["git", "-C", str(repo), *args],
                            input=input_bytes, capture_output=True,
                            env=env, check=False)
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{result.stderr.decode(errors='replace')}")
    return result.stdout.decode("utf-8", errors="replace")


def existing_fingerprints(repo: Path) -> list[tuple[str, str]]:
    """``(commit, fingerprint)`` oldest first on main."""
    if not (repo / ".git").exists():
        return []
    if not _git(repo, "rev-parse", "--verify", "-q", BRANCH,
                check=False).strip():
        return []
    out = _git(repo, "log", "--reverse", "--format=%H%x00%(trailers:key="
               "Lexgraph-Commit-Fingerprint,valueonly,separator=)%x01",
               BRANCH)
    rows = []
    for record in out.split("\x01"):
        record = record.strip()
        if not record:
            continue
        commit, _, fingerprint = record.partition("\x00")
        rows.append((commit.strip(), fingerprint.strip()))
    return rows


def _epoch(day: str) -> int:
    parsed = datetime.fromisoformat(day + "T12:00:00+00:00")
    return int(parsed.timestamp())


def fast_import_stream(commits: list[PlannedCommit], parent: str | None,
                       *, readme: bool) -> bytes:
    out: list[bytes] = []

    def data(payload: str) -> None:
        raw = payload.encode("utf-8")
        out.append(f"data {len(raw)}\n".encode() + raw + b"\n")

    for index, commit in enumerate(commits):
        out.append(f"commit {BRANCH}\n".encode())
        out.append(f"mark :{index + 1}\n".encode())
        out.append(f"author {AUTHOR} {_epoch(commit.author_date)} +0000\n"
                   .encode())
        out.append(f"committer {AUTHOR} {_epoch(commit.order_date)} +0000\n"
                   .encode())
        data(commit.message())
        if index == 0 and parent:
            out.append(f"from {parent}\n".encode())
        if index == 0 and readme:
            out.append(b"M 100644 inline README.md\n")
            data(README)
        for path in sorted(commit.files):
            content = commit.files[path]
            if content is None:
                out.append(f"D {path}\n".encode())
            else:
                out.append(f"M 100644 inline {path}\n".encode())
                data(content)
        out.append(b"\n")
    out.append(b"done\n")
    return b"".join(out)


def export(repo: Path, commits: list[PlannedCommit]) -> dict[str, Any]:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        _git(repo, "init", "-q", "-b", "main")
    existing = existing_fingerprints(repo)
    wanted = [commit.fingerprint() for commit in commits]
    common = 0
    while common < min(len(existing), len(wanted)) and \
            existing[common][1] == wanted[common]:
        common += 1
    superseded = None
    if common < len(existing):
        # Knowledge changed an earlier version: keep the old head, rebuild.
        head = existing[-1][0]
        count = len(_git(repo, "for-each-ref", "--format=%(refname)",
                         "refs/lexgraph/superseded/").split())
        superseded = f"refs/lexgraph/superseded/{count + 1}"
        _git(repo, "update-ref", superseded, head)
        if common == 0:
            _git(repo, "update-ref", "-d", BRANCH)
        else:
            _git(repo, "update-ref", BRANCH, existing[common - 1][0])
    parent = existing[common - 1][0] if common else None
    new = commits[common:]
    if new:
        stream = fast_import_stream(new, parent, readme=(common == 0))
        _git(repo, "fast-import", "--quiet", "--done",
             input_bytes=stream)
    _git(repo, "reset", "-q", "--hard", "main")
    head = _git(repo, "rev-parse", "HEAD").strip()
    return {"head": head, "commits_total": len(commits),
            "commits_reused": common, "commits_written": len(new),
            "superseded_ref": superseded}


def default_acts(data_dir: Path) -> list[str]:
    rows = json.loads((data_dir / "wiki.json").read_text(encoding="utf-8"))
    return sorted(str(row["id"]) for row in rows
                  if str(row.get("id", "")).startswith(("fed_", "by_")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path,
                        default=Path(os.environ.get(
                            "LEXGRAPH_DATA", ROOT / "web" / "data")))
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--acts", default="",
                        help="comma-separated act ids (default: all fed_/by_)")
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)
    data = args.data.resolve()
    manifest_path = data / "retrospective_history.json"
    manifest = (validate_manifest(json.loads(manifest_path.read_text(
        encoding="utf-8"))) if manifest_path.is_file() else None)
    toc = load_gii_toc(data / "gii_toc.json",
                       ROOT / "api" / "data" / "gii_toc.json")
    store = NormHistoryStore(data, manifest_loader=lambda: manifest,
                             cache_size=1)
    acts = [a for a in args.acts.split(",") if a] or default_acts(data)
    commits, report = plan(store, acts, toc)
    result = export(args.repo.expanduser(), commits)
    summary = {
        "exporter": EXPORTER,
        "exported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "data_dir": str(data),
        "retrospective_built_at": (manifest or {}).get("built_at"),
        "repo": str(args.repo.expanduser()),
        **result,
        "acts": report,
        "eu": {"status": "not_in_corpus",
               "reason": "Lexgraph's EU layer is metadata only"},
    }
    report_path = args.report or args.repo.expanduser().with_name(
        args.repo.expanduser().name + ".export.json")
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1)
                           + "\n", encoding="utf-8")
    failed = [row for row in report if row["status"] != "ok"]
    print(json.dumps({key: summary[key] for key in (
        "head", "commits_total", "commits_reused", "commits_written",
        "superseded_ref")} | {"acts": len(report), "failed_acts": len(failed)},
        ensure_ascii=False))
    return 0 if not failed else 3


if __name__ == "__main__":
    raise SystemExit(main())
