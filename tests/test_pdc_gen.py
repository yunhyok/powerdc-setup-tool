"""`core/pdc_gen.py` -- design §E `test_pdc_gen`.

The expected block text below is the spec §9 template typed out literally (with
the spec's own §4/§7 sample values substituted), *not* re-derived from the code
under test -- that is the whole point of the byte-equality assertion.
"""

from __future__ import annotations

import pytest

from powerdc_setup_tool.core.model import SinkConfig, VrmConfig
from powerdc_setup_tool.core.pdc_gen import (
    OTHER_CIRCUIT_RE,
    NegPinCache,
    estimate_block_bytes,
    estimate_insert_bytes,
    other_circuit_bytes,
    render_other_circuits,
    render_sink,
    render_vrm,
)

# spec §4 node decomposition sample: Node446!!FA134::ADC_VDD_120_VDDA_P_CL5_P/1
PNET = "ADC_VDD_120_VDDA_P_CL5_P/1"
GNET = "DGND"
VRM_POS: tuple[tuple[str, str], ...] = (("FA134", "Node446"), ("FA135", "Node447"))
VRM_NEG: tuple[tuple[str, str], ...] = (("FB001", "Node9001"), ("FB002", "Node9002"))
# spec §7 round-trip sample: 9993 $Package.Node85724!!9993::DGND on SITE1
SINK_POS: tuple[tuple[str, str], ...] = (("9910", "Node41569"),)
SINK_NEG: tuple[tuple[str, str], ...] = (("9993", "Node85724"), ("9994", "Node85725"))

EXPECTED_VRM = (
    '.VRM NominalVoltage = 1 SenseVoltage = 1 OutputCurrent = 1 Name = "VRM_LGA_'
    'ADC_VDD_120_VDDA_P_CL5_P/1_DGND"\n'
    '.Pin Name = "Positive Pin"\n'
    ".Map CircuitName = LGA CircuitPinName = FA134\n"
    ".Node Name = Node446!!FA134::ADC_VDD_120_VDDA_P_CL5_P/1\n"
    ".EndMap\n"
    ".Map CircuitName = LGA CircuitPinName = FA135\n"
    ".Node Name = Node447!!FA135::ADC_VDD_120_VDDA_P_CL5_P/1\n"
    ".EndMap\n"
    ".EndPin\n"
    '.Pin Name = "Negative Pin"\n'
    ".Map CircuitName = LGA CircuitPinName = FB001\n"
    ".Node Name = Node9001!!FB001::DGND\n"
    ".EndMap\n"
    ".Map CircuitName = LGA CircuitPinName = FB002\n"
    ".Node Name = Node9002!!FB002::DGND\n"
    ".EndMap\n"
    ".EndPin\n"
    '.Pin Name = "Positive Sense Pin"\n'
    ".EndPin\n"
    '.Pin Name = "Negative Sense Pin"\n'
    ".EndPin\n"
    ".EndVRM\n"
)

EXPECTED_SINK = (
    ".Sink NominalVoltage = 0.7 Current = 1 Model = 2 PFMode = 2 PinEqualCurrent = 1"
    ' Name = "SINK_SITE1_ADC_VDD_120_VDDA_P_CL5_P/1_DGND"\n'
    '.Pin Name = "Positive Pin"\n'
    ".Map CircuitName = SITE1 CircuitPinName = 9910\n"
    ".Node Name = Node41569!!9910::ADC_VDD_120_VDDA_P_CL5_P/1 Voltage = inf\n"
    ".EndMap\n"
    ".EndPin\n"
    '.Pin Name = "Negative Pin"\n'
    ".Map CircuitName = SITE1 CircuitPinName = 9993\n"
    ".Node Name = Node85724!!9993::DGND Voltage = inf\n"
    ".EndMap\n"
    ".Map CircuitName = SITE1 CircuitPinName = 9994\n"
    ".Node Name = Node85725!!9994::DGND Voltage = inf\n"
    ".EndMap\n"
    ".EndPin\n"
    ".SinkCurrentSource\n"
    ".EndSinkCurrentSource\n"
    ".EndSink\n"
)


def _vrm_cfg(**kwargs: object) -> VrmConfig:
    return VrmConfig(net=PNET, gnet=GNET, comp="LGA", **kwargs)  # type: ignore[arg-type]


def _sink_cfg(**kwargs: object) -> SinkConfig:
    defaults: dict[str, object] = {"nominal_voltage": 0.7}
    defaults.update(kwargs)
    return SinkConfig(net=PNET, gnet=GNET, comp="SITE1", **defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Template byte-equality (spec §9)
# ---------------------------------------------------------------------------


def test_render_vrm_matches_spec_template() -> None:
    assert render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG) == EXPECTED_VRM


def test_render_sink_matches_spec_template() -> None:
    assert render_sink(_sink_cfg(), SINK_POS, SINK_NEG) == EXPECTED_SINK


def test_blocks_are_lf_only_and_newline_terminated() -> None:
    vrm = render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG)
    sink = render_sink(_sink_cfg(), SINK_POS, SINK_NEG)
    for text in (vrm, sink):
        assert "\r" not in text
        assert text.endswith("\n")
        assert "\n\n" not in text  # spec §3: no blank lines inside a block


def test_concatenated_blocks_are_contiguous() -> None:
    # spec §3: ".EndVRM@44190671 -> .VRM@44190679 = 8 B", ".EndSink -> .Sink = 9 B".
    run = render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG) + render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG)
    assert ".EndVRM\n.VRM " in run
    sinks = render_sink(_sink_cfg(), SINK_POS, SINK_NEG) + render_sink(
        _sink_cfg(), SINK_POS, SINK_NEG
    )
    assert ".EndSink\n.Sink " in sinks
    assert ".EndVRM\n.Sink " in render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG) + sinks


def test_header_values_are_literal_engineering_values() -> None:
    # spec §4: "Header values are literal engineering values, not flags".
    header = render_vrm(
        _vrm_cfg(nominal_voltage=0.75, sense_voltage=0.7, output_current=12.5),
        VRM_POS,
        VRM_NEG,
    ).splitlines()[0]
    assert header.startswith(
        ".VRM NominalVoltage = 0.75 SenseVoltage = 0.7 OutputCurrent = 12.5 Name ="
    )


def test_sink_advanced_fields_are_carried_verbatim() -> None:
    # spec ambiguity A3: Model/PFMode/PinEqualCurrent copied verbatim, exposed as advanced.
    header = render_sink(
        _sink_cfg(model=1, pf_mode=3, pin_equal_current=0, current=2.5),
        SINK_POS,
        SINK_NEG,
    ).splitlines()[0]
    assert " Current = 2.5 Model = 1 PFMode = 3 PinEqualCurrent = 0 " in header


def test_empty_pin_sections_still_emit_their_pin_pair() -> None:
    vrm = render_vrm(_vrm_cfg(), (), ())
    assert '.Pin Name = "Positive Pin"\n.EndPin\n' in vrm
    assert '.Pin Name = "Negative Pin"\n.EndPin\n' in vrm
    sink = render_sink(_sink_cfg(), (), ())
    assert ".EndPin\n.SinkCurrentSource\n.EndSinkCurrentSource\n.EndSink\n" in sink


# ---------------------------------------------------------------------------
# `.OtherCircuit`
# ---------------------------------------------------------------------------


def test_render_other_circuits_template_and_sort_order() -> None:
    # design §D step 3: sorted by (numeric id, die).
    assert render_other_circuits(["C10132_1", "C1_1", "C2_0", "C1_0", "C10132_0"]) == (
        '.OtherCircuit Device = C1_0 Name = "C1_0"\n'
        '.OtherCircuit Device = C1_1 Name = "C1_1"\n'
        '.OtherCircuit Device = C2_0 Name = "C2_0"\n'
        '.OtherCircuit Device = C10132_0 Name = "C10132_0"\n'
        '.OtherCircuit Device = C10132_1 Name = "C10132_1"\n'
    )
    assert render_other_circuits([]) == ""


@pytest.mark.parametrize("name", ["C1_0", "C1_1", "C10132_1", "C4_0", "C99999_0"])
def test_other_circuit_re_accepts(name: str) -> None:
    assert OTHER_CIRCUIT_RE.match(name)


@pytest.mark.parametrize(
    "name",
    ["LGA", "SITE0", "SITE1", "C1", "C1_2", "ALI1", "DUT", "C_0", "CC1_0", "C1_0X", "xC1_0"],
)
def test_other_circuit_re_rejects(name: str) -> None:
    assert not OTHER_CIRCUIT_RE.match(name)


# ---------------------------------------------------------------------------
# `NegPinCache` (design §D: identical blobs, LRU <= 8)
# ---------------------------------------------------------------------------


def test_neg_pin_cache_returns_the_same_object() -> None:
    cache = NegPinCache()
    first = cache.get("LGA", GNET, "vrm", VRM_NEG)
    second = cache.get("LGA", GNET, "vrm", VRM_NEG)
    assert first is second
    assert cache.hits == 1 and cache.misses == 1


def test_neg_pin_cache_separates_styles_and_components() -> None:
    cache = NegPinCache()
    vrm_blob = cache.get("LGA", GNET, "vrm", VRM_NEG)
    sink_blob = cache.get("LGA", GNET, "sink", VRM_NEG)
    assert vrm_blob != sink_blob
    assert " Voltage = inf" not in vrm_blob  # spec §4: VRM nodes carry no Voltage
    assert sink_blob.count(" Voltage = inf") == len(VRM_NEG)  # spec §5
    assert cache.get("SITE1", GNET, "sink", SINK_NEG) is not sink_blob
    assert len(cache) == 3


def test_neg_pin_cache_is_lru_capped() -> None:
    cache = NegPinCache(maxsize=2)
    a = cache.get("A", GNET, "vrm", VRM_NEG)
    cache.get("B", GNET, "vrm", VRM_NEG)
    assert cache.get("A", GNET, "vrm", VRM_NEG) is a  # touch A -> B is now oldest
    cache.get("C", GNET, "vrm", VRM_NEG)
    assert len(cache) == 2
    assert cache.get("A", GNET, "vrm", VRM_NEG) is a  # A survived
    assert cache.get("B", GNET, "vrm", VRM_NEG) is not a  # B was evicted, re-rendered


def test_neg_pin_cache_default_capacity_and_bad_style() -> None:
    assert NegPinCache().maxsize == 8
    with pytest.raises(ValueError, match="style"):
        NegPinCache().get("LGA", GNET, "bogus", VRM_NEG)


def test_render_with_cache_is_byte_identical_to_render_without() -> None:
    cache = NegPinCache()
    assert render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG, cache=cache) == EXPECTED_VRM
    assert render_vrm(_vrm_cfg(), VRM_POS, VRM_NEG, cache=cache) == EXPECTED_VRM
    assert render_sink(_sink_cfg(), SINK_POS, SINK_NEG, cache=cache) == EXPECTED_SINK
    assert render_sink(_sink_cfg(), SINK_POS, SINK_NEG, cache=cache) == EXPECTED_SINK


# ---------------------------------------------------------------------------
# Size estimation (design §D progress denominator)
# ---------------------------------------------------------------------------


def test_estimate_block_bytes_is_exact() -> None:
    vrm = _vrm_cfg()
    sink = _sink_cfg()
    assert estimate_block_bytes(vrm, VRM_POS, VRM_NEG) == len(EXPECTED_VRM.encode("utf-8"))
    assert estimate_block_bytes(sink, SINK_POS, SINK_NEG) == len(EXPECTED_SINK.encode("utf-8"))
    assert estimate_block_bytes(vrm, (), ()) == len(
        render_vrm(vrm, (), ()).encode("utf-8")
    )


def test_estimate_block_bytes_handles_non_ascii_names() -> None:
    cfg = VrmConfig(net="ADC_VDD_070_MÜX/0", gnet=GNET, comp="LGA")
    pins = (("P1", "Node1"),)
    assert estimate_block_bytes(cfg, pins, pins) == len(
        render_vrm(cfg, pins, pins).encode("utf-8")
    )


def test_other_circuit_bytes_is_exact() -> None:
    names = ["C1_0", "C10132_1"]
    assert other_circuit_bytes(names) == len(render_other_circuits(names).encode("utf-8"))


def test_estimate_insert_bytes_totals_blocks_and_other_circuits() -> None:
    vrm = _vrm_cfg()
    sink = _sink_cfg()
    pins = {
        "VRM_LGA_" + PNET + "_DGND": (VRM_POS, VRM_NEG),
        "SINK_SITE1_" + PNET + "_DGND": (SINK_POS, SINK_NEG),
    }
    blocks = len((EXPECTED_VRM + EXPECTED_SINK).encode("utf-8"))
    assert estimate_insert_bytes([vrm, sink], pins, {"other_circuits": False}) == blocks
    assert estimate_insert_bytes(
        [vrm, sink], pins, {"other_circuits": True}, other_circuit_names=["C1_0"]
    ) == blocks + other_circuit_bytes(["C1_0"])
    # missing pin entries degrade to empty pin sections rather than raising
    assert estimate_insert_bytes([vrm], {}, {"other_circuits": False}) == len(
        render_vrm(vrm, (), ()).encode("utf-8")
    )
