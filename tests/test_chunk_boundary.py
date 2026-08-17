"""CRLF -> LF normalization across the read-chunk boundary (design §E).

Ported from `spd-model-injector`'s
`test_write_spd_normalizes_crlf_across_chunk_boundary`: shrink `_CHUNK_SIZE` to
4 so a `\\r\\n` is split between two reads, and prove the `pending_cr` carry
still collapses it to a single `\\n`.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from fixtures import build_mini_spd, expected_dc
from powerdc_setup_tool.core import writer as writer_mod
from powerdc_setup_tool.core.writer import _copy_range, _normalize_chunk, write_spd
from test_writer import _plan, _scan_fixture


# ---------------------------------------------------------------------------
# `_normalize_chunk` -- the ported primitive
# ---------------------------------------------------------------------------


def test_normalize_chunk_collapses_crlf_and_lone_cr() -> None:
    assert _normalize_chunk(b"ABC\r\nDEF\n", False) == (b"ABC\nDEF\n", False)
    assert _normalize_chunk(b"ABC\rDEF", False) == (b"ABC\nDEF", False)  # old-Mac CR
    assert _normalize_chunk(b"", False) == (b"", False)
    assert _normalize_chunk(b"plain", False) == (b"plain", False)


def test_normalize_chunk_carries_pending_cr_across_the_boundary() -> None:
    # "ABC\r" fills a 4-byte chunk exactly, so the \r\n straddles the boundary.
    first, pending = _normalize_chunk(b"ABC\r", False)
    assert (first, pending) == (b"ABC", True)
    second, pending = _normalize_chunk(b"\nDEF", pending)
    assert (second, pending) == (b"\nDEF", False)
    assert first + second == b"ABC\nDEF"


def test_normalize_chunk_pending_cr_followed_by_non_newline() -> None:
    # A lone CR at the boundary is still a line ending: it becomes exactly one \n.
    first, pending = _normalize_chunk(b"ABC\r", False)
    second, pending = _normalize_chunk(b"DEF", pending)
    assert first + second == b"ABC\nDEF"
    assert pending is False


def test_normalize_chunk_handles_a_chunk_that_is_only_cr() -> None:
    out, pending = _normalize_chunk(b"\r", False)
    assert (out, pending) == (b"", True)
    out, pending = _normalize_chunk(b"\r", pending)
    assert (out, pending) == (b"\n", True)  # the first CR's \n, second still pending


# ---------------------------------------------------------------------------
# `_copy_range` -- the old repo's byte-for-byte scenario
# ---------------------------------------------------------------------------


def test_copy_range_normalizes_crlf_across_chunk_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 4)
    source = tmp_path / "board.spd"
    output = tmp_path / "board_out.spd"
    source.write_bytes(b"ABC\r\nDEF\r\nGHI\r\n")

    with source.open("rb") as src, output.open("wb") as dst:
        pending = _copy_range(src, dst, 0, None, False)
        assert pending is False

    assert output.read_bytes() == b"ABC\nDEF\nGHI\n"


def test_copy_range_returns_pending_cr_at_a_range_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 4)
    source = tmp_path / "board.spd"
    output = tmp_path / "board_out.spd"
    source.write_bytes(b"ABC\r\nDEF\r\n")

    with source.open("rb") as src, output.open("wb") as dst:
        # stop mid-CRLF: the trailing \r is reported back, not emitted
        pending = _copy_range(src, dst, 0, 4, False)
        assert pending is True
        assert _copy_range(src, dst, 4, None, pending) is False

    assert output.read_bytes() == b"ABC\nDEF\n"


# ---------------------------------------------------------------------------
# Whole-file: a CRLF fixture spliced with a 4-byte chunk window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("style", ["si", "dc"])
def test_write_spd_normalizes_crlf_fixture_across_chunk_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 4)
    source = tmp_path / "in.spd"
    build_mini_spd(source, style=style, newline="crlf")  # type: ignore[arg-type]
    assert b"\r\n" in source.read_bytes()

    scan = _scan_fixture(source, style=style, newline="crlf")
    out = tmp_path / "out.spd"
    write_spd(scan, _plan(scan), out)

    data = out.read_bytes()
    assert b"\r" not in data
    assert data == expected_dc().encode("utf-8")


@pytest.mark.parametrize("style", ["si", "dc"])
def test_write_spd_normalizes_lone_cr_line_endings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, style: str
) -> None:
    # An old-Mac CR-only file: every \r becomes \n, chunk boundaries included.
    # `style="dc"` also pins the `.OtherCircuit` filter, whose line-oriented copy
    # sees the whole range as one CR-run "line" until it is normalized.
    monkeypatch.setattr(writer_mod, "_CHUNK_SIZE", 4)
    lf_source = tmp_path / "lf.spd"
    build_mini_spd(lf_source, style=style)  # type: ignore[arg-type]
    scan = _scan_fixture(lf_source, style=style)  # \n and \r are both 1 byte: same offsets

    cr_source = tmp_path / "in.spd"
    cr_source.write_bytes(lf_source.read_bytes().replace(b"\n", b"\r"))
    assert b"\n" not in cr_source.read_bytes()

    out = tmp_path / "out.spd"
    write_spd(dataclasses.replace(scan, path=cr_source), _plan(scan), out)

    data = out.read_bytes()
    assert b"\r" not in data
    assert data == expected_dc().encode("utf-8")
