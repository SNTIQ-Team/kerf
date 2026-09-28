"""The Git export view: one file per norm, one commit per version.

Built from the real captured states in tests/fixtures/law_history.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from api.norm_history import NormHistoryStore
from api.norm_identity import load_gii_toc
from api.retrospective_store import validate_manifest
from tools.law_git_export import export, plan

FIXTURE = Path(__file__).parent / "fixtures" / "law_history"
ACTS = ["fed_asylvfg_1992", "fed_sgb_14", "by_agsg"]

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary required")


def _plan(data: Path, *, drop_last_state: bool = False):
    raw = json.loads((data / "retrospective_history.json").read_text())
    if drop_last_state:
        # Simulate the earlier knowledge before 2026-07-29: the newest state
        # and its interval were not yet known.
        newest = "f74d8bf27b8e9afe78c5a4e76fd2cb713d3e020e8f0de56f22e234bc3008c502"
        act = raw["acts"]["fed_asylvfg_1992"]
        act["intervals"] = [row for row in act["intervals"]
                            if row["state_sha256"] != newest]
        path = data / "acts" / "fed_asylvfg_1992.json"
        payload = json.loads(path.read_text())
        payload["official_states"] = [
            row for row in payload["official_states"]
            if row["state_sha256"] != newest]
        path.write_text(json.dumps(payload))
    manifest = validate_manifest(raw)
    store = NormHistoryStore(data, manifest_loader=lambda: manifest)
    return plan(store, ACTS, load_gii_toc(data / "gii_toc.json"))


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture
def data(tmp_path):
    target = tmp_path / "web-data"
    shutil.copytree(FIXTURE, target)
    return target


def test_one_commit_per_version_dated_by_valid_from(data, tmp_path):
    commits, report = _plan(data)
    assert all(row["status"] == "ok" for row in report)
    repo = tmp_path / "law-git"
    result = export(repo, commits)
    log = _git(repo, "log", "--reverse", "--date=short",
               "--format=%ad|%s|%(trailers:key=Lexgraph-Author-Date-Basis,"
               "valueonly,separator=)", "--",
               "de/bund/asylvfg_1992/_act.md").splitlines()
    assert [line.split("|")[0] for line in log] == [
        "2026-07-06", "2026-07-10", "2026-10-01"]
    assert log[0].endswith("|observed_at")
    assert "legal start unresolved" in log[0]
    assert "BGBl. 2026 I Nr. 199" in log[1] and log[1].endswith("|valid_from")
    assert "in force 2026-10-01" in log[2]
    message = _git(repo, "log", "-1", "--format=%B", "--grep",
                   "BGBl. 2026 I Nr. 199")
    assert "Commencement basis: official_bgbl_command_and_commencement_clause" \
        in message
    assert "Lexgraph-Amending-Document: bgbl-1-2026-199" in message
    assert result["commits_written"] == len(commits)


def test_norm_file_front_matter_and_sentence_lines(data, tmp_path):
    commits, _ = _plan(data)
    repo = tmp_path / "law-git"
    export(repo, commits)
    text = (repo / "de/bund/sgb_14/para/37.md").read_text()
    front, body = text.split("\n---\n", 1)
    assert 'id: "de/bund/sgb_14/para/37"' in front
    assert 'version_id: "de/bund/sgb_14/para/37@observed-2026-07-06#' in front
    assert "valid_from: null" in front
    assert 'valid_from_status: "unresolved"' in front
    assert 'source_role: "official_consolidation_gii"' in front
    assert 'source_url: "https://www.gesetze-im-internet.de/sgb_14/__37.html"' \
        in front
    assert "content_sha256:" in front and "text_sha256:" in front
    assert '"absatz": 2' in front and '"nummer": 6' in front
    lines = body.strip().splitlines()
    assert "3. die Anforderungen an die personelle Ausstattung und an die " \
        "Qualifikation des Personals," in lines
    assert not any(line.startswith("(3)") for line in lines)


def test_blame_is_per_sentence(data, tmp_path):
    commits, _ = _plan(data)
    repo = tmp_path / "law-git"
    export(repo, commits)
    blame = _git(repo, "blame", "--date=short",
                 "de/bund/asylvfg_1992/para/29a.md")
    changed = next(line for line in blame.splitlines() if "(2a)" in line)
    assert "2026-07-10" in changed
    unchanged = next(line for line in blame.splitlines()
                     if line.rstrip().endswith("(2) Sichere Herkunftsstaaten im "
                                               "Sinne des Artikels 16a Absatz 3 "
                                               "Satz 1 des Grundgesetzes sind "
                                               "die in Anlage II bezeichneten "
                                               "Staaten.")
                     or "(2) Sichere Herkunftsstaaten" in line)
    assert "2026-07-06" in unchanged


def test_export_is_idempotent_and_incremental(data, tmp_path):
    repo = tmp_path / "law-git"
    early, _ = _plan(data, drop_last_state=True)
    first = export(repo, early)
    assert first["commits_written"] == len(early)
    # Fresh copy with the complete knowledge.
    full_data = tmp_path / "full"
    shutil.copytree(FIXTURE, full_data)
    full, _ = _plan(full_data)
    second = export(repo, full)
    assert second["commits_reused"] == len(early)
    assert second["commits_written"] == len(full) - len(early)
    assert second["superseded_ref"] is None
    third = export(repo, full)
    assert third["commits_written"] == 0 and third["head"] == second["head"]
    # A clean rebuild yields the same history, bit for bit.
    rebuilt = export(tmp_path / "rebuilt", full)
    assert rebuilt["head"] == third["head"]


def test_changed_knowledge_rewrites_only_from_the_changed_version(
        data, tmp_path):
    repo = tmp_path / "law-git"
    commits, _ = _plan(data)
    before = export(repo, commits)
    commits[-1].body += "\nCorrected evidence."
    after = export(repo, commits)
    assert after["commits_reused"] == len(commits) - 1
    assert after["superseded_ref"] == "refs/lexgraph/superseded/1"
    assert _git(repo, "rev-parse", after["superseded_ref"]).strip() == \
        before["head"]
