# SPD Manipulator for PowerDC

[![Build](https://github.com/yunhyok/powerdc-setup-tool/actions/workflows/build.yml/badge.svg)](https://github.com/yunhyok/powerdc-setup-tool/actions/workflows/build.yml)

**SPD Manipulator for PowerDC** is a Windows desktop tool (PySide6) that pre-configures a Cadence
Sigrity PowerSI `.spd` design for PowerDC (DCR) analysis: classify nets, pair power rails to their
return (ground) nets, set per-net voltages, and generate the `.VRM` / `.Sink` blocks PowerDC
expects -- then stream out a new `.spd` file, leaving the original untouched.

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
  across a selection, fill down/right, TSV copy/paste, checkbox toggling, all undoable.
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
