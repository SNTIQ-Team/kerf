"""Filesystem guard for bulk writes (btrfs metadata exhaustion on /home).

Rules (operator instructions, 2026-09-28):
* never *start* bulk writes (sync, reparse, capture migration) while btrfs
  "Device unallocated" is below 10 GiB;
* stop a running sync when metadata usage rises above 90 %.

"Metadata usage" is measured as used / (allocated + allocatable), where
allocatable = unallocated / metadata ratio (DUP = 2): metadata chunks can only
grow into unallocated space.  With ~1 MiB unallocated this equals the plain
used/total ratio; after a data balance it reflects the real headroom.  Both
ratios are reported.  Non-btrfs filesystems only need MIN_FREE_OTHER bytes free.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIB = 1024 ** 3
GB = 1000 ** 3
MIN_UNALLOCATED_TO_START = 10 * GIB
MIN_UNALLOCATED_WHILE_RUNNING = 5 * GIB   # courts.writer checks this (and the WAL) every batch
MAX_METADATA_USED = 0.90
MIN_FREE_OTHER = 50 * GB
MIN_BTRFS_FREE_TO_START = 60 * GB         # data space ("Free (estimated)"); runs stop below 50 GB
MIN_BTRFS_FREE_WHILE_RUNNING = 50 * GB


@dataclass
class SpaceReport:
    path: str
    filesystem: str
    unallocated: int | None = None
    metadata_used: int | None = None
    metadata_total: int | None = None
    metadata_ratio: float = 2.0
    free: int | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def metadata_used_fraction(self) -> float | None:
        if not self.metadata_total:
            return None
        return self.metadata_used / self.metadata_total

    @property
    def metadata_effective_fraction(self) -> float | None:
        if not self.metadata_total:
            return None
        growable = (self.unallocated or 0) / (self.metadata_ratio or 1)
        return self.metadata_used / (self.metadata_total + growable)

    def as_dict(self) -> dict:
        return {"path": self.path, "filesystem": self.filesystem, "unallocated_bytes": self.unallocated,
                "unallocated_gib": round(self.unallocated / GIB, 2) if self.unallocated is not None else None,
                "metadata_used_bytes": self.metadata_used, "metadata_total_bytes": self.metadata_total,
                "metadata_used_fraction": _round(self.metadata_used_fraction),
                "metadata_effective_fraction": _round(self.metadata_effective_fraction),
                "free_bytes": self.free, "problems": self.problems}


def _round(value):
    return round(value, 4) if value is not None else None


def filesystem_type(path: Path) -> str:
    try:
        out = subprocess.run(["stat", "-f", "-c", "%T", str(path)], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def parse_btrfs(usage_text: str, df_text: str, report: SpaceReport) -> SpaceReport:
    m = re.search(r"Device unallocated:\s+(\d+)", usage_text)
    report.unallocated = int(m.group(1)) if m else None
    m = re.search(r"Free \(estimated\):\s+(\d+)", usage_text)
    report.free = int(m.group(1)) if m else None
    m = re.search(r"Metadata ratio:\s+([\d.]+)", usage_text)
    report.metadata_ratio = float(m.group(1)) if m else 2.0
    m = re.search(r"^Metadata, \w+: total=(\d+), used=(\d+)", df_text, re.M)
    if m:
        report.metadata_total, report.metadata_used = int(m.group(1)), int(m.group(2))
    return report


def measure(path: Path) -> SpaceReport:
    path = Path(path)
    probe = path if path.exists() else next((p for p in path.parents if p.exists()), Path("/"))
    report = SpaceReport(str(probe), filesystem_type(probe))
    if report.filesystem == "btrfs":
        try:
            usage = subprocess.run(["btrfs", "filesystem", "usage", "-b", str(probe)], capture_output=True,
                                   text=True, timeout=60).stdout
            df = subprocess.run(["btrfs", "filesystem", "df", "-b", str(probe)], capture_output=True,
                                text=True, timeout=60).stdout
            parse_btrfs(usage, df, report)
        except (OSError, subprocess.SubprocessError) as exc:
            report.problems.append(f"btrfs tools unavailable: {exc}")
        if report.unallocated is None or report.metadata_total is None:
            report.problems.append("could not read btrfs allocation; refusing bulk writes")
    else:
        report.free = shutil.disk_usage(probe).free
    return report


def evaluate(report: SpaceReport, *, starting: bool) -> SpaceReport:
    if report.filesystem == "btrfs":
        if report.unallocated is not None:
            floor = MIN_UNALLOCATED_TO_START if starting else MIN_UNALLOCATED_WHILE_RUNNING
            if report.unallocated < floor:
                report.problems.append(f"btrfs Device unallocated {report.unallocated / GIB:.2f} GiB < "
                                       f"{floor / GIB:.0f} GiB (run: sudo btrfs balance start -dusage=20 /home)")
        if report.free is not None:
            floor = MIN_BTRFS_FREE_TO_START if starting else MIN_BTRFS_FREE_WHILE_RUNNING
            if report.free < floor:
                report.problems.append(f"btrfs Free (estimated) {report.free / GIB:.1f} GiB < {floor / GIB:.0f} GiB")
        fraction = report.metadata_effective_fraction
        if fraction is not None and fraction > MAX_METADATA_USED:
            report.problems.append(f"btrfs metadata {fraction:.1%} used (> {MAX_METADATA_USED:.0%})")
    elif report.free is not None and report.free < MIN_FREE_OTHER:
        report.problems.append(f"only {report.free / GIB:.2f} GiB free")
    return report


def check(path: Path, *, starting: bool = True) -> SpaceReport:
    if os.environ.get("SNTIQ_COURTS_SPACE_GUARD") == "off":  # tests only
        return SpaceReport(str(path), "guard-disabled")
    return evaluate(measure(path), starting=starting)
