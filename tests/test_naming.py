"""`core/naming.py` -- design §E `test_naming`; spec §2 palette, §6 naming rules."""

from __future__ import annotations

import pytest

from powerdc_setup_tool.core.naming import (
    COLOR_CYCLE,
    color_for,
    die_of,
    guess_voltage,
    sink_component,
    sink_name,
    vrm_name,
)

# spec §6: "Observed 050 055 070 075 105 120 180", <ccc>/100 = volts.
VOLTAGE_TABLE: tuple[tuple[str, float], ...] = (
    ("ADC_VDD_050_VDDQ_DDRH/0", 0.50),
    ("ADC_VDD_055_VTRIP/0", 0.55),
    ("ADC_VDD_070_VP_HSIO/0", 0.70),
    ("ADC_VDD_075_VTRIP_SRAM/0", 0.75),
    ("ADC_VDD_105_VP_CORE/1", 1.05),
    ("ADC_VDD_120_VDDA_P_CL5_P/1", 1.20),
    ("ADC_VDD_180_VP_IO/1", 1.80),
)

# Names with no confident three-digit VDD code (design §A: "return None when no
# confident match"); `Session` falls back to 1.0 V for these (design §B).
UNMATCHED_NETS: tuple[str, ...] = (
    "DGND",
    "VSS",
    "SIG_CLK_IN",
    "W_DDR9_BP_C0_DQ[8]/1",
    "ADC_VDD_07_VTRIP/0",  # two digits
    "ADC_VDD_0700_VTRIP/0",  # four digits
    "ADC_VDDX_070_VTRIP/0",  # code not attached to VDD
    "ADC_VDD070_VTRIP/0",  # missing separator
    "",
)


@pytest.mark.parametrize(("net", "volts"), VOLTAGE_TABLE)
def test_guess_voltage_table(net: str, volts: float) -> None:
    assert guess_voltage(net) == pytest.approx(volts)


@pytest.mark.parametrize("net", UNMATCHED_NETS)
def test_guess_voltage_returns_none_when_unmatched(net: str) -> None:
    assert guess_voltage(net) is None


def test_guess_voltage_matches_without_die_suffix_and_at_name_start() -> None:
    assert guess_voltage("ADC_VDD_120_VDDB") == pytest.approx(1.20)
    assert guess_voltage("VDD_070_VP_HSIO/0") == pytest.approx(0.70)
    assert guess_voltage("ADC_VDD_070") == pytest.approx(0.70)


def test_guess_voltage_is_none_when_two_codes_disagree() -> None:
    # Ambiguous is not confident -- better to fall back than to pick the wrong rail.
    assert guess_voltage("ADC_VDD_070_MUX_VDD_120_X/0") is None
    assert guess_voltage("ADC_VDD_070_MUX_VDD_070_X/0") == pytest.approx(0.70)


@pytest.mark.parametrize(
    ("net", "die"),
    [
        ("ADC_VDD_070_VDDA/0", 0),
        ("ADC_VDD_120_VDDB/1", 1),
        ("W_DDR9_BP_C0_DQ[8]/1", 1),
        ("DGND", None),
        ("ADC_VDD_070_VDDA/", None),
        ("ADC_VDD_070_VDDA/0/", None),
        ("", None),
    ],
)
def test_die_of(net: str, die: int | None) -> None:
    assert die_of(net) == die


def test_block_names() -> None:
    # spec §6: VRM_{board-side comp}_{power net incl. /d}_{ground net}.
    assert vrm_name("LGA", "ADC_VDD_120_VDDA_P_CL5_P/1", "DGND") == (
        "VRM_LGA_ADC_VDD_120_VDDA_P_CL5_P/1_DGND"
    )
    assert sink_name("SITE1", "ADC_VDD_120_VDDA_P_CL5_P/1", "DGND") == (
        "SINK_SITE1_ADC_VDD_120_VDDA_P_CL5_P/1_DGND"
    )


def test_sink_component() -> None:
    # spec §6: "SINK_SITE0 <=> net suffix /0, SINK_SITE1 <=> /1 (46/46, 0 exceptions)".
    assert sink_component(0) == "SITE0"
    assert sink_component(1) == "SITE1"
    assert sink_component(die_of("ADC_VDD_120_VDDB/1")) == "SITE1"
    assert sink_component(None) == "SITE0"  # documented default for die-less nets


def test_color_cycle() -> None:
    # spec §2: "Colors cycle over 11 names".
    assert COLOR_CYCLE == (
        "RED",
        "GREEN",
        "YELLOW",
        "OLIVE",
        "FUCHSIA",
        "DARKRED",
        "DARKMAGENTA",
        "DARKGREEN",
        "DARKCYAN",
        "DARKBLUE",
        "BLUE",
    )
    assert len(set(COLOR_CYCLE)) == 11
    assert color_for(0) == "RED"
    assert color_for(10) == "BLUE"
    assert color_for(11) == "RED"
    assert color_for(23) == color_for(1) == "GREEN"
    assert [color_for(i) for i in range(11)] == list(COLOR_CYCLE)
