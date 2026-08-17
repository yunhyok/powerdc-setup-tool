"""Tests for `core/netlist.py` -- design §E "test_netlist" row.

The load-bearing guarantee is byte-identity: `render_netlist` must reproduce the
parsed body exactly when no classification actually changes (design §G.7's
"round-trip identity test is the guard"). Everything else asserts that a real
classification change edits *only* the affected lines, per the spec §9 netlist
templates.
"""

from __future__ import annotations

import pytest

import fixtures
from powerdc_setup_tool.core.model import PowerNetConfig
from powerdc_setup_tool.core.netlist import (
    GROUP_NODES,
    NETLIST_COLORS,
    parse_netlist,
    render_netlist,
)
from powerdc_setup_tool.core.spd_scan import scan_spd

BODY = "".join(line + "\n" for line in fixtures._netlist_entries())

POWER_A = fixtures.POWER_NET_A  # classified power, group's first child
POWER_B = fixtures.POWER_NET_B  # classified power, inherits the group
GROUND = fixtures.GROUND_NET  # classified ground, its group's first child
PLAIN_A, PLAIN_B = fixtures.UNCLASSIFIED_NETS


def cfg(net: str, net_class: str, **kwargs) -> PowerNetConfig:
    return PowerNetConfig(net=net, net_class=net_class, **kwargs)


def as_is(entries) -> dict[str, PowerNetConfig]:
    """A config dict that asks for exactly what the body already says."""
    out: dict[str, PowerNetConfig] = {}
    for e in entries:
        if not e.name or e.name in GROUP_NODES:
            continue
        net_class = {"PowerNets": "power", "GroundNets": "ground"}.get(e.group, "none")
        out[e.name] = cfg(e.name, net_class)
    return out


def lines(text: str) -> list[str]:
    return text.split("\n")[:-1]


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #


def test_parse_fields():
    entries = parse_netlist(BODY)
    assert len(entries) == len(fixtures._netlist_entries())
    by_name = {e.name: e for e in entries}

    root = entries[0]
    assert (root.name, root.group, root.group_explicit) == ("", None, False)
    assert (root.sel_state, root.view_mode) == ("Unselected", "DropShape")
    assert root.attrs == (("RiseTime", "0ps"), ("%Coupling", "0"))
    assert root.index == 0

    group_node = by_name["PowerNets"]
    assert (group_node.group, group_node.group_explicit) == (None, False)
    assert group_node.attrs == (("Color", "RED"),)
    assert (group_node.sel_state, group_node.view_mode) == (None, None)

    plain = by_name[PLAIN_A]
    assert (plain.group, plain.group_explicit) == (None, False)
    assert (plain.sel_state, plain.view_mode) == ("Unselected", "DropShape")
    assert plain.attrs == (("Color", "GREEN"),)

    ground = by_name[GROUND]
    assert (ground.group, ground.group_explicit) == ("GroundNets", True)
    assert (ground.sel_state, ground.view_mode) == (None, None)
    assert ground.attrs == (("Color", "GREEN"), ("Voltage", "0"))

    first_power = by_name[POWER_A]
    assert (first_power.group, first_power.group_explicit) == ("PowerNets", True)
    assert first_power.attrs == (("Color", "RED"),)

    # spec §2: subsequent children inherit the last named parent positionally
    later_power = by_name[POWER_B]
    assert (later_power.group, later_power.group_explicit) == ("PowerNets", False)
    assert later_power.attrs == (("Color", "OLIVE"),)

    # the trailing out-of-order L1 block is L1 because it carries "::sel||view"
    sense = by_name[fixtures.SENSE_NETS[0]]
    assert (sense.group, sense.group_explicit) == (None, False)
    assert (sense.sel_state, sense.view_mode) == ("Unselected", "DropShape")

    assert [e.index for e in entries] == list(range(len(entries)))
    assert [e.raw for e in entries] == fixtures._netlist_entries()


def test_render_entry_reproduces_every_raw_line():
    """Canonical rendering == the source line, for every entry of the body."""
    from powerdc_setup_tool.core.netlist import _render_entry

    for e in parse_netlist(BODY):
        assert _render_entry(e) == e.raw


def test_parse_tolerates_opaque_lines():
    body = "\tA::Unselected||DropShape Color = RED\n\n\tB::Unselected||DropShape Color = BLUE\n"
    entries = parse_netlist(body)
    assert [e.raw for e in entries] == ["\tA::Unselected||DropShape Color = RED", "", "\tB::Unselected||DropShape Color = BLUE"]
    assert render_netlist(entries, {}) == body


def test_parse_empty_body():
    assert parse_netlist("") == []
    assert render_netlist([], {}) == ""


# --------------------------------------------------------------------------- #
# byte-identity round trips
# --------------------------------------------------------------------------- #


def test_round_trip_is_byte_identical_without_configs():
    assert render_netlist(parse_netlist(BODY), {}) == BODY


def test_round_trip_is_byte_identical_with_unchanged_configs():
    """The guard from design §G.7: same classification in == same bytes out."""
    entries = parse_netlist(BODY)
    assert render_netlist(entries, as_is(entries)) == BODY
    assert render_netlist(entries, as_is(entries), emit_groups=False) == BODY
    assert render_netlist(entries, as_is(entries), emit_power_voltage=True) == BODY


def test_untouched_lines_are_emitted_verbatim_not_re_rendered():
    """Byte-identity comes from re-emitting `raw`, not from canonical rendering.

    The fixture body happens to be exactly canonical, so this uses a body whose
    spacing a canonical re-render would "fix" -- untouched lines must survive
    it byte-for-byte anyway (design §G.7 "never regenerate untouched lines").
    """
    from powerdc_setup_tool.core.netlist import _render_entry

    body = (
        "\t::Unselected||DropShape RiseTime = 0ps %Coupling = 0\n"
        "\tPowerNets  Color  =  RED\n"  # doubled spaces
        "\tODD_NET::Unselected||DropShape Color = GREEN   \n"  # trailing blanks
        "\tA_RAIL/0 -> PowerNets Color = RED\n"
        "\tB_RAIL/1 Color = OLIVE\n"
    )
    entries = parse_netlist(body)
    assert entries[4].group == "PowerNets"  # positional inheritance still works
    assert _render_entry(entries[1]) != entries[1].raw  # canonical != source
    assert _render_entry(entries[2]) != entries[2].raw

    assert render_netlist(entries, {}) == body
    assert render_netlist(entries, as_is(entries)) == body

    out = render_netlist(entries, {"ODD_NET": cfg("ODD_NET", "power")})
    assert "\tPowerNets  Color  =  RED\n" in out  # untouched, warts and all
    assert out.endswith("\tODD_NET Color = GREEN\n")  # rewritten line is canonical


@pytest.mark.parametrize("style", ["si", "dc"])
@pytest.mark.parametrize("newline", ["lf", "crlf"])
def test_round_trip_from_a_scanned_fixture(tmp_path, style, newline):
    path = tmp_path / f"{style}_{newline}.spd"
    fixtures.build_mini_spd(path, style=style, newline=newline)
    scan = scan_spd(path)

    assert scan.netlist_body == BODY
    entries = list(scan.nets)
    assert render_netlist(entries, {}) == scan.netlist_body
    assert render_netlist(entries, as_is(entries)) == scan.netlist_body


# --------------------------------------------------------------------------- #
# classification edits
# --------------------------------------------------------------------------- #


def test_classify_power_moves_line_and_drops_selstate():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {PLAIN_A: cfg(PLAIN_A, "power")}))

    # the old L1 line is gone ...
    assert f"\t{PLAIN_A}::Unselected||DropShape Color = GREEN" not in out
    # ... and reappears at the end of the PowerNets member run, unselected part
    # dropped (spec §A7), no "-> PowerNets" (it is not the first child).
    assert f"\t{PLAIN_A} Color = GREEN" in out
    assert out.index(f"\t{PLAIN_A} Color = GREEN") == out.index(f"\t{POWER_B} Color = OLIVE") + 1

    # every other line is untouched, in order
    untouched = [line for line in out if PLAIN_A not in line]
    assert untouched == [line for line in fixtures._netlist_entries() if PLAIN_A not in line]

    # power members carry no Voltage (spec §2)
    assert "Voltage" not in f"\t{PLAIN_A} Color = GREEN"


def test_classify_ground_adds_voltage_zero():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {PLAIN_B: cfg(PLAIN_B, "ground")}))

    assert f"\t{PLAIN_B} Color = YELLOW Voltage = 0" in out
    # inserted at the end of the GroundNets run (DGND is its only member)
    assert out.index(f"\t{PLAIN_B} Color = YELLOW Voltage = 0") == out.index(
        f"\t{GROUND} -> GroundNets Color = GREEN Voltage = 0"
    ) + 1


def test_classify_multiple_nets_sorts_the_new_members():
    entries = parse_netlist(BODY)
    configs = {
        PLAIN_B: cfg(PLAIN_B, "power"),
        PLAIN_A: cfg(PLAIN_A, "power"),
        fixtures.SENSE_NETS[0]: cfg(fixtures.SENSE_NETS[0], "power"),
    }
    out = lines(render_netlist(entries, configs))
    added = [line for line in out if any(net in line for net in configs)]
    assert added == [
        f"\t{fixtures.SENSE_NETS[0]} Color = DARKCYAN",
        f"\t{PLAIN_A} Color = GREEN",
        f"\t{PLAIN_B} Color = YELLOW",
    ]
    assert out.index(added[0]) == out.index(f"\t{POWER_B} Color = OLIVE") + 1


def test_declassify_appends_at_end_and_promotes_next_first_child():
    """Removing a group's first child promotes the next member (spec §2 "->")."""
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {POWER_A: cfg(POWER_A, "none")}))

    assert f"\t{POWER_A} -> PowerNets Color = RED" not in out
    assert out[-1] == f"\t{POWER_A}::Unselected||DropShape Color = RED"
    # POWER_B was an inheriting member; it is now the run's first child
    assert f"\t{POWER_B} -> PowerNets Color = OLIVE" in out
    assert f"\t{POWER_B} Color = OLIVE" not in out
    # ground run untouched
    assert f"\t{GROUND} -> GroundNets Color = GREEN Voltage = 0" in out


def test_declassify_ground_drops_voltage():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {GROUND: cfg(GROUND, "none")}))
    assert out[-1] == f"\t{GROUND}::Unselected||DropShape Color = GREEN"
    assert not any(line.startswith(f"\t{GROUND}") and "Voltage" in line for line in out)


def test_power_to_ground_reclassification():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {POWER_B: cfg(POWER_B, "ground")}))

    assert f"\t{POWER_B} Color = OLIVE Voltage = 0" in out
    assert out.index(f"\t{POWER_B} Color = OLIVE Voltage = 0") == out.index(
        f"\t{GROUND} -> GroundNets Color = GREEN Voltage = 0"
    ) + 1
    # POWER_A stays the PowerNets first child, untouched
    assert f"\t{POWER_A} -> PowerNets Color = RED" in out


def test_emit_power_voltage_opt_in():
    entries = parse_netlist(BODY)
    configs = {PLAIN_A: cfg(PLAIN_A, "power", voltage=1.25)}
    assert f"\t{PLAIN_A} Color = GREEN" in lines(render_netlist(entries, configs))
    assert f"\t{PLAIN_A} Color = GREEN Voltage = 1.25" in lines(
        render_netlist(entries, configs, emit_power_voltage=True)
    )


# --------------------------------------------------------------------------- #
# group nodes
# --------------------------------------------------------------------------- #


def _body_without_group_nodes() -> str:
    return "".join(
        line + "\n"
        for line in fixtures._netlist_entries()
        if line not in ("\tGroundNets Color = LIME", "\tPowerNets Color = RED")
    )


def test_group_node_inserted_at_ascii_sort_position():
    body = _body_without_group_nodes()
    entries = parse_netlist(body)
    out = lines(render_netlist(entries, {PLAIN_A: cfg(PLAIN_A, "power")}))

    assert "\tPowerNets Color = RED" in out
    assert "\tGroundNets Color = LIME" not in out  # nothing arrived in GroundNets
    # ASCII position: after the root, before SIG_CLK_IN (the sorted L1 run)
    assert out.index("\tPowerNets Color = RED") == out.index(
        f"\t{PLAIN_B}::Unselected||DropShape Color = YELLOW"
    ) - 1
    assert out.index("\tPowerNets Color = RED") < out.index(f"\t{PLAIN_A} Color = GREEN")


def test_both_group_nodes_inserted_in_ascii_order():
    body = _body_without_group_nodes()
    entries = parse_netlist(body)
    out = lines(
        render_netlist(
            entries,
            {PLAIN_A: cfg(PLAIN_A, "power"), PLAIN_B: cfg(PLAIN_B, "ground")},
        )
    )
    assert out.index("\tGroundNets Color = LIME") + 1 == out.index("\tPowerNets Color = RED")
    assert GROUP_NODES == {"PowerNets": "RED", "GroundNets": "LIME"}


def test_emit_groups_false_suppresses_group_nodes():
    body = _body_without_group_nodes()
    entries = parse_netlist(body)
    out = lines(render_netlist(entries, {PLAIN_A: cfg(PLAIN_A, "power")}, emit_groups=False))
    assert "\tPowerNets Color = RED" not in out
    assert f"\t{PLAIN_A} Color = GREEN" in out


def test_existing_group_node_is_never_duplicated():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {PLAIN_A: cfg(PLAIN_A, "power")}))
    assert out.count("\tPowerNets Color = RED") == 1


def test_group_nodes_are_never_reclassified():
    """A stray config named after a group node must not rewrite it."""
    entries = parse_netlist(BODY)
    out = render_netlist(entries, {"PowerNets": cfg("PowerNets", "power")})
    assert out == BODY


# --------------------------------------------------------------------------- #
# nets that are not in the body at all
# --------------------------------------------------------------------------- #


def test_net_absent_from_body_is_appended_to_its_group_run():
    entries = parse_netlist(BODY)
    out = lines(render_netlist(entries, {"NEW_RAIL/0": cfg("NEW_RAIL/0", "power")}))
    added = f"\tNEW_RAIL/0 Color = {NETLIST_COLORS[len(entries) % len(NETLIST_COLORS)]}"
    assert added in out
    assert out.index(added) == out.index(f"\t{POWER_B} Color = OLIVE") + 1
    assert len(out) == len(fixtures._netlist_entries()) + 1


def test_unclassified_net_absent_from_body_is_ignored():
    entries = parse_netlist(BODY)
    assert render_netlist(entries, {"NEW_RAIL/0": cfg("NEW_RAIL/0", "none")}) == BODY


def test_first_member_of_an_empty_group_gets_the_arrow():
    body = "".join(
        line + "\n"
        for line in fixtures._netlist_entries()
        if not line.startswith(f"\t{GROUND} ")
    )
    entries = parse_netlist(body)
    out = lines(render_netlist(entries, {PLAIN_B: cfg(PLAIN_B, "ground")}))
    assert f"\t{PLAIN_B} -> GroundNets Color = YELLOW Voltage = 0" in out
    # appended after the end of the combined member region
    assert out.index(f"\t{PLAIN_B} -> GroundNets Color = YELLOW Voltage = 0") == out.index(
        f"\t{POWER_B} Color = OLIVE"
    ) + 1
