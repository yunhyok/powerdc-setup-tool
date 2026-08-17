"""End-to-end performance envelope -- design §E `test_perf`, §G.5 targets.

design §E: *"200 MB synthetic file: scan < 60 s, write < 120 s, RSS < 256 MB"*.
`tests/fixtures.py:build_scaled_spd` produces that file with the real design's
proportions (see `tests/test_real_extracts.py` for where those come from):
thousands of nets, 92 power rails, an LGA with ~5 k pins, two die sites, and a
`.PowerSI` `.Port` section carrying the bulk of the bytes.

Two tests, one shape:

* `test_scaled_pipeline_sanity` -- ~20 MB, loose bounds, **runs in CI**. It is
  the gross-regression alarm: an accidental "read the whole file into a list"
  shows up here as a 10x, long before anyone runs the slow test.
* `test_full_scale_pipeline` -- ~200 MB, the design §E numbers, `@slow`.

Both drive the whole pipeline (`scan_spd` -> `Session` -> `build_plan` ->
`write_spd`) **in a child process**, because `resource.getrusage` reports a
process-wide high-water mark: measured in-process it would also count pytest,
PySide6 and every other test module's imports, and could never be compared
against a 256 MB budget. The child reports its own `ru_maxrss`, so the number
this test asserts is the number the app would show on its own.

`resource` is Unix-only; on Windows (where CI runs the non-slow selection) the
child reports `rss_kb = None` and only the timing assertions apply.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fixtures import build_scaled_spd

#: design §E / §G.5 targets for the full-size file.
SCAN_BUDGET_S = 60.0
WRITE_BUDGET_S = 120.0
RSS_BUDGET_MB = 256.0

#: Loose bounds for the CI-sized run -- an order of magnitude of head-room, so
#: the test flags a real regression rather than a slow build agent.
SANITY_BUDGET_S = 15.0

SRC = Path(__file__).resolve().parent.parent / "src"

# The child: scan -> Session -> build_plan -> write_spd, then report. Kept as a
# script (not a helper import) so nothing this test module imports can pollute
# the peak-RSS measurement.
_WORKER = textwrap.dedent(
    """
    import json, sys, time
    from pathlib import Path

    from powerdc_setup_tool.core.session import Session
    from powerdc_setup_tool.core.spd_scan import scan_spd
    from powerdc_setup_tool.core.writer import write_spd

    source, output = Path(sys.argv[1]), Path(sys.argv[2])

    scan_seen, write_seen = [], []

    t0 = time.monotonic()
    scan = scan_spd(source, progress=lambda d, t: scan_seen.append((d, t)))
    scan_s = time.monotonic() - t0

    session = Session()
    session.load(scan)
    blocking = [i.message for i in session.validate() if i.blocking]
    plan = session.build_plan()

    t0 = time.monotonic()
    write_spd(scan, plan, output, progress=lambda d, t: write_seen.append((d, t)))
    write_s = time.monotonic() - t0

    def monotonic(seen):
        return all(a[0] <= b[0] for a, b in zip(seen, seen[1:]))

    try:
        import resource
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:            # Windows: no getrusage
        rss_kb = None

    with open(output, "rb") as handle:
        head = handle.read(4096)
        handle.seek(0, 2)
        out_size = handle.tell()

    print(json.dumps({
        "scan_s": scan_s,
        "write_s": write_s,
        "rss_kb": rss_kb,
        "in_size": scan.file_size,
        "out_size": out_size,
        "nets": len(scan.nets),
        "vrms": len(plan.vrms),
        "sinks": len(plan.sinks),
        "netlist_rewritten": plan.netlist_body is not None,
        "blocking": blocking,
        "scan_progress_calls": len(scan_seen),
        "write_progress_calls": len(write_seen),
        "progress_monotonic": monotonic(scan_seen) and monotonic(write_seen),
        "scan_progress_complete": bool(scan_seen) and scan_seen[-1][0] == scan.file_size,
        "workflow_key_patched": b"0x1000000067" in head,
    }))
    """
)


def _run_pipeline(source: Path, output: Path) -> dict:
    """Run the scan+write pipeline in a child process; return its report."""
    # Inherit the environment (Windows python needs SYSTEMROOT et al.) and only
    # add the import path, so this works on the CI runners too.
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = subprocess.run(
        [sys.executable, "-c", _WORKER, str(source), str(output)],
        capture_output=True,
        text=True,
        env=env,
        timeout=SCAN_BUDGET_S + WRITE_BUDGET_S + 300,
        check=False,
    )
    assert proc.returncode == 0, f"pipeline failed:\n{proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _check_common(report: dict, stats: dict) -> None:
    """Assertions that hold at every scale -- correctness, not speed."""
    assert report["blocking"] == [], f"synthetic fixture must validate clean: {report['blocking']}"
    assert report["vrms"] == report["sinks"] == stats["power_nets"]
    assert report["nets"] == stats["nets"] + 3  # + root entry + 2 group nodes
    # design §G.1: an already-classified netlist is copied, never re-rendered.
    assert report["netlist_rewritten"] is False
    # design §D step 1 / spec §A9
    assert report["workflow_key_patched"] is True
    # design §G.5: progress fires on both phases, never goes backwards, and the
    # scan's last call reports the whole file (what drives the UI progress bar).
    # The <=20 Hz throttle means a fast small run legitimately reports only the
    # mandatory first/last pair.
    assert report["scan_progress_calls"] >= 2
    assert report["write_progress_calls"] >= 2
    assert report["progress_monotonic"] is True
    assert report["scan_progress_complete"] is True
    # The output is the input plus the generated block run.
    assert report["out_size"] > report["in_size"]


def _report(label: str, report: dict, stats: dict) -> str:
    mb_in = report["in_size"] / 1e6
    mb_out = report["out_size"] / 1e6
    rss = report["rss_kb"]
    text = (
        f"\n[{label}] in {mb_in:,.1f} MB -> out {mb_out:,.1f} MB "
        f"({stats['power_nets']} rails, {stats['lga_pins']} LGA pins)\n"
        f"  scan  {report['scan_s']:6.2f} s  ({mb_in / max(report['scan_s'], 1e-9):7.1f} MB/s)\n"
        f"  write {report['write_s']:6.2f} s  ({mb_out / max(report['write_s'], 1e-9):7.1f} MB/s)\n"
        f"  peak RSS {'n/a' if rss is None else f'{rss / 1024:.1f} MB'}\n"
    )
    print(text)
    return text


def _assert_rss(report: dict) -> None:
    rss_kb = report["rss_kb"]
    if rss_kb is None:  # Windows child: no getrusage
        return
    assert rss_kb / 1024 < RSS_BUDGET_MB, (
        f"peak RSS {rss_kb / 1024:.1f} MB exceeds the design §D/§E {RSS_BUDGET_MB} MB budget"
    )


def test_scaled_pipeline_sanity(tmp_path: Path) -> None:
    """~20 MB end to end with loose bounds -- the CI gross-regression alarm."""
    source = tmp_path / "sanity.spd"
    stats = build_scaled_spd(
        source, target_bytes=20 * 1024 * 1024, nets=3000, lga_pins=1500, site_pins=600
    )
    assert 15e6 < stats["bytes"] < 30e6

    report = _run_pipeline(source, tmp_path / "sanity_DC.spd")
    _report("20 MB sanity", report, stats)
    _check_common(report, stats)

    assert report["scan_s"] < SANITY_BUDGET_S
    assert report["write_s"] < SANITY_BUDGET_S
    _assert_rss(report)


@pytest.mark.slow
def test_full_scale_pipeline(tmp_path: Path) -> None:
    """design §E: 200 MB synthetic -- scan < 60 s, write < 120 s, RSS < 256 MB."""
    source = tmp_path / "big.spd"
    stats = build_scaled_spd(
        source, target_bytes=200 * 1024 * 1024, nets=3000, lga_pins=5000, site_pins=2000
    )
    assert 190e6 < stats["bytes"] < 215e6
    assert stats["power_nets"] == 92 and stats["lga_pins"] == 5000

    report = _run_pipeline(source, tmp_path / "big_DC.spd")
    _report("200 MB full scale", report, stats)
    _check_common(report, stats)

    assert report["scan_s"] < SCAN_BUDGET_S, (
        f"scan took {report['scan_s']:.1f} s, budget {SCAN_BUDGET_S} s"
    )
    assert report["write_s"] < WRITE_BUDGET_S, (
        f"write took {report['write_s']:.1f} s, budget {WRITE_BUDGET_S} s"
    )
    _assert_rss(report)

    # Machine-independent proof that both passes really stream (design §D
    # "blocks are streamed to disk as rendered; the 213 MB is never
    # materialized"): peak RSS must stay far below the file it just processed.
    if report["rss_kb"] is not None:
        assert report["rss_kb"] * 1024 < report["in_size"] / 2, (
            "peak RSS is within 2x of the input size -- something is buffering "
            "the file instead of streaming it"
        )


@pytest.mark.slow
def test_full_scale_crlf_input_yields_lf_only_contiguous_output(tmp_path: Path) -> None:
    """design §G.6 (LF-only output) and §D step 5 (no blank line between blocks),
    on a 200 MB **CRLF** input.

    `test_chunk_boundary.py` pins `_normalize_chunk` with a 4-byte chunk; this is
    the same property at the production 1 MB chunk size, over ~1 M real chunk
    boundaries -- the only place a `\\r\\n` split by the read window would show up.
    """
    source = tmp_path / "big_crlf.spd"
    output = tmp_path / "big_crlf_DC.spd"
    stats = build_scaled_spd(
        source,
        target_bytes=200 * 1024 * 1024,
        nets=3000,
        lga_pins=5000,
        site_pins=2000,
        newline="crlf",
    )
    assert source.read_bytes()[:4096].count(b"\r\n") > 10, "fixture is not CRLF"

    report = _run_pipeline(source, output)
    _check_common(report, stats)

    carry = b""
    blocks = 0
    with output.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            assert b"\r" not in chunk, "output must be LF-only (design §G.6)"
            window = carry + chunk
            blocks += window.count(b"\n.EndVRM\n.VRM ") + window.count(b"\n.EndSink\n.Sink ")
            assert b"\n.EndVRM\n\n" not in window, "blank line between blocks (spec §3)"
            assert b"\n.EndSink\n\n.Sink" not in window
            carry = window[-32:]
    # 92 VRMs + 92 Sinks contiguous => 91 + 91 adjacent pairs.
    assert blocks == 2 * (stats["power_nets"] - 1)
