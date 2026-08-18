# SPD Manipulator for PowerDC

[![Build](https://github.com/yunhyok/powerdc-setup-tool/actions/workflows/build.yml/badge.svg)](https://github.com/yunhyok/powerdc-setup-tool/actions/workflows/build.yml)

**SPD Manipulator for PowerDC** is a Windows desktop tool (PySide6) that pre-configures a Cadence
Sigrity PowerSI `.spd` design for PowerDC (DCR) analysis: classify nets, pair power rails to their
return (ground) nets, set per-net voltages, and generate the `.VRM` / `.Sink` blocks PowerDC
expects -- then stream out a new `.spd` file, leaving the original untouched.

## v0.1.3

Maintenance release: v0.1.2 built and ran fine locally but its Windows release job never finished,
so no v0.1.2 installer was ever published. Behaviour is unchanged from v0.1.2 apart from the fixes
below.

- **CI hang fix** -- bulk edits (type-to-fill, fill down/right, paste, *Check/Uncheck all (shown)*,
  *Classify*, undo/redo) now suspend the sort/filter proxies' dynamic re-sorting while they write
  and re-sort once at the end, instead of letting every single cell write re-sort and re-filter the
  table underneath the operation. *Check/Uncheck all (shown)* also resolves the shown rows before
  it writes the first one. Same results, no rows moving mid-operation, and large bulk edits are
  much faster.
- **Sort comparator made a strict weak ordering** -- the v0.1.2 comparator mixed numeric and text
  comparison within one column and treated `NaN` as equal to everything, either of which is
  undefined behaviour inside the C++ sort Qt hands it to (a possible non-terminating sort, and one
  that can behave differently on Windows than on Linux). Values are now bucketed and never compared
  across buckets, with the input file's row order as a final tie-break.
- **Per-test timeouts** -- the test suite fails a hung test after 180 s (600 s for the `slow`
  200 MB perf tests) with a full thread dump, and the release job as a whole is capped at 30
  minutes, so a hang shows up as a named failing test within minutes instead of a silent six-hour
  kill with no logs.
- **Qt pinned to `PySide6>=6.7,<6.12`** -- release builds no longer pick up an untested newer Qt
  minor on their own.

## v0.1.2

- **Column-header sorting on all three tables** -- click any header to sort ascending/descending,
  with the usual indicator. Numeric columns (voltage, current, pins, die) sort *numerically*, not
  as text, and sorting is purely view state: bulk edits, the context menu, checkbox toggles,
  override styling, *Go to net* and undo/redo all keep addressing the rows you picked, whatever
  order the table is in. Tables still open in the input file's own net order.
- **Check all (shown) / Uncheck all (shown)** -- two new Net Manager context-menu entries that set
  the *Use* checkbox for every row the filter is currently showing (not just the selected ones), as
  a single undoable step with a status-bar count. *Check/Uncheck selected* is unchanged.
- **Paired GND combo fix** -- the drop-down could come up empty, leaving nothing to pick. It is now
  built from the session's ground nets *at the moment the editor opens*, always with a blank entry
  (to clear the pairing) and the row's own current value, so a net just classified as ground is
  offered immediately and the combo is never a dead end.
- **Auto-classification on load** -- opening a `.spd` now runs the name-based classification and
  ground pairing automatically, before the tables are first drawn, so an unclassified design shows
  its power/ground classes, pairings and VRM/Sink rows right away. Classification the input file
  already carries is still preloaded first and is never overridden. The *Auto-classify* toolbar
  button stays, for re-runs. The status bar reports
  `Loaded: N nets · auto-classified +P power +G ground`.
- **Source column** -- now says where a net's class actually came from: `input` (the loaded
  `.NetList` classified it), `auto` (the tool's own name-based/pairing pass did), `user` (you did),
  or blank while the net is unclassified. It is saved in the config JSON, and configs written by
  older versions still load.

## v0.1.1

- **Right-click Classify** -- the Net Manager's context menu now opens with PowerSI's own
  *Classify > as PowerNets / as GroundNets / as Signal Nets*, applied to the whole selection as a
  single undoable step. The separate Mark Power / Mark Ground / Clear class button row is gone
  (*Set voltage…* and *Set paired ground…* live in the same menu).
- **Name-based auto-classification** -- *Auto-classify* now also classifies the nets the input file
  left unclassified from their names (`..._VDD_...`/`VCC`/`VPP`/... -> power, `GND`/`AGND`/`VSS`/
  `GROUND` -> ground), leaving signal nets, `_PS`/`_GS` sense nets, rail status signals
  (`PWR_GOOD`, `VDD_EN`) and every net the `.NetList` already classified untouched, then re-runs
  ground pairing. New power nets pick up their name-derived voltage, default ground pairing and
  VRM/Sink rows exactly as a manual classification would.

## What it does

- **Net selection & classification** -- Power / Ground / Unclassified, in a filterable table that
  preloads existing classification straight from the input file's `.NetList` when present.
- **Power/ground (P/G) pairing** -- auto-picks the most likely paired ground net per power net (the
  ground net with the most pins on the VRM component), overridable per row or for a whole selection.
- **Per-net voltage with auto-guess from net names** -- `ADC_VDD_<ccc>_...` -> `ccc / 100` V, editable
  per row; edits to the net voltage propagate down into the derived VRM/Sink fields until a derived
  cell is itself overridden.
- **VRM and Sink generation** -- one `.VRM` / `.Sink` block per selected power net, with editable
  nominal voltage, sense voltage (VRM), and output/sink current per row.
- **Multi-row / multi-cell bulk editing** on all three tables (Net Manager, VRMs, Sinks): type-to-fill
  across a selection, fill down/right, TSV copy/paste, checkbox toggling, all undoable -- and
  click-to-sort on every column header, with numeric columns sorted as numbers.
- **Streaming, 1.4 GB-safe rewrite** -- input files around 1.4 GB (output around 1.6 GB) are scanned
  and rewritten with a single forward streaming pass and bounded memory use; the file is never
  loaded fully into RAM.
- **LF-only output** -- Cadence requires LF-only `.spd` files; output is normalized to LF even when
  the source uses CRLF, including safely across streaming chunk boundaries.
- **Never overwrites the source** -- export always writes to a new path (default `<name>_DC.spd`
  next to the source); the tool refuses to write back over the file it read.

## Screenshots

_(Screenshots will be added here once the UI is finalized.)_

## Install

**Option 1 -- installer (recommended for end users).** Download the latest
`PowerDC-Setup-Tool-Setup-<version>.exe` from the [Releases](https://github.com/yunhyok/powerdc-setup-tool/releases)
page and run it. This installs the app under Program Files with a Start Menu entry and an optional
desktop icon.

**Option 2 -- editable install (for development).**

```powershell
git clone https://github.com/yunhyok/powerdc-setup-tool.git
cd powerdc-setup-tool
python -m pip install -e ".[dev]"
powerdc-setup-tool
```

Requires Python 3.12+.

## Usage

1. **Open SPD** -- load a PowerSI-exported (or already PowerDC-converted) `.spd` file. The app makes
   a single streaming pass over the file to index nets, pins, and any existing VRM/Sink blocks.
2. **Net Manager** -- classify nets as Power/Ground, confirm or override P/G pairing, and set
   per-net voltage.
3. **VRMs / Sinks** -- review the generated rows, adjust nominal voltage/current per row, or
   bulk-edit a selection.
4. **Export DC SPD** -- validate, pick an output path (never the source path), and write. Progress is
   shown while the new `.spd` streams to disk.

## Building locally

```powershell
.\scripts\build.ps1
```

This runs the test suite (`pytest -m "not slow"`), builds a onedir PyInstaller bundle from
`packaging/powerdc-setup-tool.spec`, then compiles the Inno Setup installer from
`packaging/powerdc-setup-tool.iss` (resolving `ISCC.exe` from the usual Inno Setup 6 install
location, PATH, or a Chocolatey install as a last resort). Useful switches:

- `-Version <version>` -- version string baked into the installer filename and metadata.
- `-SkipTests` -- skip the pytest run.
- `-SkipInstaller` -- stop after the PyInstaller build, skipping Inno Setup.

## CI & releases

[`.github/workflows/build.yml`](.github/workflows/build.yml) runs on every push of a `v*` tag (and
can be run manually via `workflow_dispatch`). On `windows-latest` it installs the project, runs the
test suite (`QT_QPA_PLATFORM=offscreen`), builds the PyInstaller bundle and Inno Setup installer,
uploads the installer as a build artifact, and -- for tag pushes only -- publishes a GitHub Release
with the installer attached via `gh release create`. `scripts/publish_release.ps1` does the
equivalent from a local checkout (creates the GitHub repo if needed, pushes, and creates the release).

## Tests

```bash
QT_QPA_PLATFORM=offscreen pytest -m "not slow"   # what CI runs
pytest -m slow                                   # 200 MB synthetic perf envelope
```

Two of them need extra context:

- **`tests/test_perf.py`** builds a scaled synthetic `.spd` (`tests/fixtures.py:build_scaled_spd`;
  ~200 MB, 92 rails, ~5k LGA pins, the real design's section proportions) and drives
  scan -> `Session` -> `write_spd` in a child process, asserting the design targets: scan < 60 s,
  write < 120 s, peak RSS < 256 MB. A ~20 MB version of the same run stays in the default selection
  so CI catches gross regressions.
- **`tests/test_real_extracts.py`** cross-validates the generated `.VRM` / `.Sink` / `.NetList` /
  `.OtherCircuit` text byte-for-byte against staged extracts of a real Cadence design. Those
  extracts are customer data and are **never committed**; the module skips itself when they are
  absent (point `POWERDC_REAL_EXTRACTS` at a directory of them to run it).

## File-format notes

Full grammar lives in the internal `spd_dc_format_spec.md` design notes; short summary:

- **`.NetList`** -- a serialized 2-level tree of nets. Power/Ground membership is expressed purely by
  membership in the built-in `PowerNets` / `GroundNets` group nodes; a classified net drops its
  `::Unselected||DropShape` suffix.
- **`.PowerDC`** -- the large DC-analysis section. Holds `.OtherCircuit` discrete-component entries,
  an empty `.SpiceNetlist` placeholder, then every `.VRM` block followed by every `.Sink` block,
  contiguous with no blank lines between them.
- **`.VRM` / `.Sink`** -- one block per power net, each with Positive/Negative `.Pin` sections mapping
  circuit pins to nodes; `.Sink` nodes additionally carry `Voltage = inf` and have no sense pins.
- **`.Connect ... .EndC`** -- per-component pin -> node -> net tables (one block per placed
  component). This is the authoritative pin source used to build the VRM/Sink `.Map` entries.

## 한국어 요약

- **SPD Manipulator for PowerDC**는 Cadence Sigrity PowerSI에서 내보낸 대용량 `.spd` 파일(최대 약
  1.4GB)을 PowerDC(DCR) 해석용으로 준비해주는 윈도우 데스크톱 도구입니다.
- 넷(net) 목록을 불러와 전력/접지로 분류하고, 전력 넷마다 짝이 되는 접지 넷을 자동으로 추천하며,
  넷 이름에서 전압을 자동으로 추정합니다 (예: `ADC_VDD_070_...` → 0.7V).
- 이 정보를 바탕으로 `.VRM` / `.Sink` 블록을 생성하고, 표에서 여러 행·셀을 한 번에 선택해 값을
  채우거나 붙여넣는 일괄 편집(bulk edit)을 지원합니다.
- 원본 파일은 스트리밍 방식으로 읽고 써서 메모리 사용량을 낮게 유지하며, 출력은 항상 LF 개행만
  사용하고 원본 파일은 절대 덮어쓰지 않습니다.
- 설치는 Releases 페이지의 설치 프로그램을 사용하거나, 개발 시 `pip install -e ".[dev]"`로 소스에서
  바로 실행할 수 있습니다.
- 사용 순서: **SPD 열기** → **Net Manager**에서 분류·페어링·전압 설정 → **VRMs/Sinks** 탭에서 값
  확인 및 수정 → **Export DC SPD**로 새 파일 저장.

## License

MIT License, Copyright (c) 2026 Yunhyok. See [LICENSE](LICENSE).
