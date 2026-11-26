"""Archive worker archive offload: receipts, attestations, VPS retention.

The VPS may delete a byte only when a signed archive worker receipt lists its
exact path and SHA-256, and only if the daily refresh no longer reads it.
"""
from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                reason="ssh-keygen required for signatures")

receipts = importlib.import_module("archive_receipts")
storage = importlib.import_module("snapshot_storage")
offload = importlib.import_module("archive_offload")
legacy = importlib.import_module("backfill_legacy_bgbl_history")

TODAY = date.today()


def _day(delta: int) -> str:
    return (TODAY - timedelta(days=delta)).isoformat()


@pytest.fixture
def signer(tmp_path, monkeypatch):
    key = tmp_path / "keys" / "signing"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                    receipts.PRINCIPAL, "-f", str(key)], check=True)
    public = key.with_suffix(".pub").read_text().strip()
    signers = tmp_path / "keys" / "allowed_signers"
    signers.write_text(
        f'{receipts.PRINCIPAL} namespaces="{receipts.RECEIPT_NAMESPACE},'
        f'{receipts.ATTESTATION_NAMESPACE}" {public}\n')
    monkeypatch.setenv("KERF_ARCHIVE_SIGNERS", str(signers))
    return key


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _legacy_store(data: Path) -> dict:
    """A tiny pre-2023 store: one PDF, one text, one pinned document."""
    store = data / "legacy_bgbl_history"
    pdf, text = b"%PDF-1.4 legacy", "Bundesgesetzblatt 1999\nArtikel 1\n".encode()
    paths = {}
    for folder, payload, suffix in (("objects", pdf, ".pdf"),
                                    ("texts", text, ".txt")):
        digest = _sha(payload)
        path = store / folder / digest[:2] / f"{digest}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        paths[folder] = (path.relative_to(store).as_posix(), digest)
    (store / "checkpoint.json").write_text('{"schema_version":1}\n')
    return paths


def _snapshots(data: Path) -> Path:
    root = data / "snapshots"
    for age in (40, 30, 20, 2, 1):
        path = root / "dip" / _day(age) / "vorgaenge.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"age": age, "pad": "x" * 200}) + "\n")
    return root


def _cli(*args, data: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "archive_offload.py"),
         "--data-root", str(data), "--offload-root", str(data / "archive-offload"),
         *args], capture_output=True, text=True, check=False)
    assert result.returncode in (0, 3), result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def _receipt(data: Path, laptop: Path, key: Path) -> str:
    """Manifest on the 'VPS', copy to the 'archive worker', signed receipt back."""
    info = _cli("manifest", data=data)
    for folder in ("legacy_bgbl_history", "snapshots"):
        if (data / folder).is_dir():
            shutil.copytree(data / folder, laptop / folder, dirs_exist_ok=True)
    manifest = data / "archive-offload" / "manifests" / info["manifest"]
    out = laptop.parent / "offload" / "receipts"
    _cli("receipt", "--manifest", str(manifest), "--archive", str(laptop),
         "--key", str(key), "--out", str(out), "--allow-excluded", data=data)
    target = data / "archive-offload" / "receipts"
    target.mkdir(parents=True, exist_ok=True)
    name = f"{manifest.stem}.receipt.json"
    for payload in (manifest, out / name):
        fields = json.loads(payload.read_bytes())
        assert {"host", "workstation", "archive", "data_root"}.isdisjoint(fields)
        assert str(data.parent).encode() not in payload.read_bytes()
    for suffix in ("", ".sig"):
        shutil.copy2(out / (name + suffix), target / (name + suffix))
    return name


def _attested_legacy(data: Path, key: Path, rows: list[dict], day: str) -> Path:
    snap = data / "snapshots" / "bgbl_legacy_history_backfill" / day
    snap.mkdir(parents=True, exist_ok=True)
    (snap / "candidates.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    (snap / "documents.jsonl").write_text("{}\n")
    (snap / "summary.json").write_text('{"verified_documents": 1}')
    attestation = receipts.build_attestation(
        snap, repo_root=ROOT, rebuilt_at="2026-09-28T00:00:00Z", receipt=None,
        git_commit=None, verified_documents=1)
    receipts.write_json_atomic(snap / receipts.ATTESTATION_NAME, attestation)
    receipts.sign_file(snap / receipts.ATTESTATION_NAME, key,
                       receipts.ATTESTATION_NAMESPACE)
    return snap


# ------------------------------------------------------------------ receipts

def test_receipt_covers_only_verified_bytes_and_rejects_tampering(
        tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    paths = _legacy_store(data)
    info = _cli("manifest", data=data)
    assert info["files"] == 3
    shutil.copytree(data / "legacy_bgbl_history", laptop / "legacy_bgbl_history")
    # The archive worker copy of the text differs: it must be excluded, not receipted.
    (laptop / "legacy_bgbl_history" / paths["texts"][0]).write_bytes(b"torn")
    manifest = data / "archive-offload" / "manifests" / info["manifest"]
    out = tmp_path / "laptop" / "offload" / "receipts"
    result = _cli("receipt", "--manifest", str(manifest), "--archive",
                  str(laptop), "--key", str(signer), "--out", str(out),
                  data=data)
    assert result["excluded"] == 1 and result["verified_files"] == 2
    target = data / "archive-offload" / "receipts"
    shutil.copytree(out, target)
    hashed = receipts.receipted_hashes(data / "archive-offload")
    pdf_key = f"legacy_bgbl_history/{paths['objects'][0]}"
    assert hashed[pdf_key] == {paths["objects"][1]}
    assert f"legacy_bgbl_history/{paths['texts'][0]}" not in hashed
    # A second manifest lists only what is still unreceipted.
    assert _cli("manifest", data=data)["files"] == 1
    # Tampering with a signed receipt withdraws all its permissions.
    receipt = next(target.glob("*.receipt.json"))
    value = json.loads(receipt.read_text())
    value["excluded"] = []
    receipt.write_text(json.dumps(value))
    assert receipts.receipted_hashes(data / "archive-offload") == {}


# ------------------------------------------------------------------ pruning

def test_legacy_sources_stay_until_the_latest_snapshot_is_attested(
        tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    paths = _legacy_store(data)
    (data / "snapshots").mkdir()
    _receipt(data, laptop, signer)
    blocked = _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"),
                   data=data)
    assert blocked["legacy_source_files"] == 0
    assert "not attested" in blocked["blocked"]["legacy_sources"] or \
        "no legacy" in blocked["blocked"]["legacy_sources"]
    assert (data / "legacy_bgbl_history" / paths["objects"][0]).is_file()

    _attested_legacy(data, signer, [{"id": "row-1"}], _day(0))
    result = _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"),
                  data=data)
    assert result["legacy_source_files"] == 2 and result["blocked"] == {}
    assert not (data / "legacy_bgbl_history" / paths["objects"][0]).exists()
    assert (data / "legacy_bgbl_history" / "checkpoint.json").is_file()
    # The archive worker still holds every pruned byte.
    assert (laptop / "legacy_bgbl_history" / paths["objects"][0]).is_file()


def test_unreceipted_legacy_bytes_are_never_pruned(tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    paths = _legacy_store(data)
    (data / "snapshots").mkdir()
    _receipt(data, laptop, signer)
    _attested_legacy(data, signer, [{"id": "row-1"}], _day(0))
    # Bytes captured after the receipt have no receipt yet.
    late = data / "legacy_bgbl_history" / "objects" / "ff" / ("f" * 64 + ".pdf")
    late.parent.mkdir(parents=True)
    late.write_bytes(b"new capture")
    # A receipted path whose bytes changed afterwards is not covered either.
    (data / "legacy_bgbl_history" / paths["texts"][0]).write_bytes(b"changed")
    result = _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"),
                  data=data)
    assert result["legacy_source_files"] == 1
    assert late.is_file()
    assert (data / "legacy_bgbl_history" / paths["texts"][0]).is_file()


def test_compacted_objects_leave_after_the_window_and_stay_restorable(
        tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    root = _snapshots(data)
    storage.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    _receipt(data, laptop, signer)
    result = _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"),
                  data=data)
    # 40 and 30 days are outside the 14-day window; 20 days too; the two
    # newest days stay raw and were never compacted.
    assert result["compact_objects"] == 3
    report = storage.verify_archive(root)
    assert report["offloaded_objects"] == 3
    old = root / "dip" / _day(40) / "vorgaenge.jsonl"
    with pytest.raises(storage.SnapshotArchiveError, match="offloaded"):
        with storage.open_snapshot_text(old):
            pass
    # The archive worker copy is complete: its compaction archive restores it.
    with storage.open_snapshot_text(laptop / "snapshots" / "dip" / _day(40)
                                    / "vorgaenge.jsonl") as stream:
        assert json.loads(stream.readline())["age"] == 40


def test_protected_gii_days_keep_their_objects(tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    root = data / "snapshots"
    for age in (40, 30, 2, 1):
        for name in ("acts.jsonl", "norms.jsonl"):
            path = root / "gii" / _day(age) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"age": age, "name": name}) + "\n")
    storage.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    _receipt(data, laptop, signer)
    result = _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"),
                  data=data)
    # Old GII days with both files are still archived by the daily refresh.
    assert result["compact_objects"] == 0


def test_identical_bytes_after_offload_are_stored_again(tmp_path, signer):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    root = _snapshots(data)
    storage.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    _receipt(data, laptop, signer)
    _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"), data=data)
    same = (root / "dip" / _day(1) / "vorgaenge.jsonl")
    # Bytes identical to an offloaded object reappear in a new aged-out day.
    payload = json.dumps({"age": 40, "pad": "x" * 200}) + "\n"
    for age in (0,):
        path = root / "dip" / _day(age) / "vorgaenge.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    same.write_text(payload)
    result = storage.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert result["compacted_files"] >= 1
    storage.verify_archive(root)
    with storage.open_snapshot_text(root / "dip" / _day(2)
                                    / "vorgaenge.jsonl") as stream:
        assert json.loads(stream.readline())["age"] == 2


# ------------------------------------------------------- legacy attestation

def test_build_admits_exactly_the_attested_candidate_rows(
        tmp_path, signer, monkeypatch):
    data = tmp_path / "vps"
    family = data / "snapshots" / "bgbl_legacy_history_backfill"
    row = {"id": "legacy-1", "act_id": "fed_x", "commands": [{"raw": "§ 1"}]}
    _attested_legacy(data, signer, [row], _day(0))
    monkeypatch.setattr(legacy, "SNAPSHOT_ROOT", family)
    legacy.validate_legacy_candidate(dict(row))
    with pytest.raises(legacy.SourceIntegrityError, match="not in the"):
        legacy.validate_legacy_candidate({**row, "act_id": "fed_y"})
    # A changed attested file invalidates the attestation as a whole.
    (family / _day(0) / "candidates.jsonl").write_text("{}\n")
    with pytest.raises(legacy.SourceIntegrityError, match="attestation failed"):
        legacy.validate_legacy_candidate(dict(row))
    assert "not valid" in legacy.attestation_notice()


def test_capture_only_accepts_receipted_absent_sources_only(
        tmp_path, signer, monkeypatch):
    data, laptop = tmp_path / "vps", tmp_path / "laptop" / "data"
    paths = _legacy_store(data)
    (data / "snapshots").mkdir()
    store = legacy.LegacyArchive(data / "legacy_bgbl_history", min_free_bytes=0)
    document = {"pdf_object": paths["objects"][0], "sha256": paths["objects"][1],
                "text_object": paths["texts"][0],
                "text_sha256": paths["texts"][1], "year": "1999"}
    evidence = legacy._json_bytes(document)
    document["legacy_document_object"] = store.put("documents", evidence, ".json")
    document["legacy_document_sha256"] = legacy._digest(evidence)
    _receipt(data, laptop, signer)
    _attested_legacy(data, signer, [{"id": "r"}], _day(0))
    _cli("prune", "--apply", "--lock-file", str(tmp_path / "lock"), data=data)
    assert not (data / "legacy_bgbl_history" / paths["objects"][0]).exists()
    assert legacy._verified_pinned(document, store)["year"] == "1999"
    # An absent source without a receipt is an integrity failure.
    shutil.rmtree(data / "archive-offload" / "receipts")
    with pytest.raises(FileNotFoundError):
        legacy._verified_pinned(document, store)


def test_uncapped_archive_requires_archive_mode():
    with pytest.raises(ValueError, match="archive-only"):
        legacy.LegacyArchive(Path("/srv/x/legacy"), max_bytes=None)


def test_install_supersedes_a_same_day_snapshot_without_deleting_it(
        tmp_path, signer, monkeypatch):
    data = tmp_path / "vps"
    family = data / "snapshots" / "bgbl_legacy_history_backfill"
    existing = family / _day(0)
    existing.mkdir(parents=True)
    (existing / "candidates.jsonl").write_text('{"id":"vps-built"}\n')
    staging = tmp_path / "laptop"
    snap = _attested_legacy(staging, signer, [{"id": "laptop-built"}], _day(0))
    incoming = data / "archive-offload" / "incoming" / "20260928T000000Z"
    incoming.parent.mkdir(parents=True)
    shutil.copytree(snap, incoming)
    monkeypatch.setattr(offload, "DATA", data)
    assert offload.main(["--data-root", str(data), "install-legacy",
                         "--lock-file", str(tmp_path / "lock"),
                         str(incoming)]) == 0
    installed = family / _day(0)
    assert json.loads((installed / "candidates.jsonl").read_text())["id"] == \
        "laptop-built"
    superseded = list((family / ".superseded").iterdir())
    assert len(superseded) == 1
    assert "vps-built" in (superseded[0] / "candidates.jsonl").read_text()
    receipts.verify_attestation(installed)
