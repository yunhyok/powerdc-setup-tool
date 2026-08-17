"""`.NetList` grammar parse + minimal-edit re-render -- owned by chunk 1 (design §A;
spec §2).

Round-trips byte-identical when no `PowerNetConfig` changes are applied. When
a net's classification changes, only that net's own line (and, for a newly
classified member, its insertion point at the end of its group's existing
member run) is rewritten -- never re-sort, never regenerate untouched lines
(design §G.7).

Grammar (spec §2)::

    netentry ::= TAB name [ " -> " group ] [ "::" selstate "||" viewmode ]
                 ( SP KEY " = " VALUE )*

The section is a serialized 2-level tree: `PowerNets`/`GroundNets` are plain L1
"group node" entries, and membership is expressed by ``-> Group`` on the
group's **first** child plus positional inheritance for the rest.

Resolved ambiguities (pinned by the tests):

* **Where a member run ends.** Spec §2 states a classified net "drops the
  ``::selstate||viewmode`` part entirely", so the presence of that part is taken
  as proof the line is *not* a group member: such a line is L1 and clears the
  inherited group. That is what makes the file's trailing out-of-order L1 block
  (the 184 sense nets) parse as L1 even though it follows the 93-member run.
* **Ground voltage.** Spec §9's template shows ``Voltage = 0`` on the ground
  group's *first* child; semantically it is a per-net DC voltage, so **every**
  ground member gets it (with one ground net -- the real-file case -- the two
  readings coincide).
* **Power voltage.** Spec §2 is explicit that the 92 power members carry **no**
  ``Voltage =`` (it lives in `.VRM`/`.Sink NominalVoltage`), so that is the
  default. ``emit_power_voltage=True`` opts into writing
  ``Voltage = {cfg.voltage}`` on rewritten power members for callers that want
  it; it never touches untouched lines.
* **Insertion.** New *members* go at the end of their group's existing member
  run (design §G.7, never re-sorted). A missing *group node* is inserted at its
  ASCII-sort position among the sorted L1 run (design §E). A net that becomes
  unclassified is re-emitted at the end of the body -- exactly the shape the
  real file shows (spec §2 "L1, appended out of order").
"""

from __future__ import annotations

from dataclasses import replace

from powerdc_setup_tool.core.model import NetEntry, PowerNetConfig

__all__ = [
    "NETLIST_COLORS",
    "GROUP_NODES",
    "parse_netlist",
    "render_netlist",
]

# spec §2: "Colors cycle over 11 names."
NETLIST_COLORS: tuple[str, ...] = (
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

#: The two built-in group nodes and the colour the spec §9 template gives each.
GROUP_NODES: dict[str, str] = {"PowerNets": "RED", "GroundNets": "LIME"}

_POWER = "PowerNets"
_GROUND = "GroundNets"

_SEL_UNSELECTED = "Unselected"
_VIEW_DROPSHAPE = "DropShape"

_ENTRY_PREFIX = "\t"
_GROUP_SEP = " -> "


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def _parse_attrs(text: str) -> tuple[tuple[str, str], ...]:
    """``" Color = RED Voltage = 0"`` -> ``(("Color", "RED"), ("Voltage", "0"))``.

    Tolerates quoted values containing spaces; anything that does not fit the
    ``KEY = VALUE`` shape is dropped from :attr:`NetEntry.attrs` (the verbatim
    line survives in :attr:`NetEntry.raw`, which is what untouched lines emit).
    """
    tokens = text.split()
    attrs: list[tuple[str, str]] = []
    count = len(tokens)
    i = 0
    while i + 2 < count:
        if tokens[i + 1] != "=":
            i += 1
            continue
        key = tokens[i]
        value = tokens[i + 2]
        i += 3
        if value.startswith('"') and not value.endswith('"'):
            while i < count:
                value += " " + tokens[i]
                i += 1
                if value.endswith('"'):
                    break
        attrs.append((key, value))
    return tuple(attrs)


def _split_head(text: str) -> tuple[str, str | None, bool, str | None, str | None, str]:
    """Split one entry line body into (name, group, explicit, sel, view, attrs_text)."""
    group: str | None = None
    explicit = False
    if _GROUP_SEP in text:
        head, _, after = text.partition(_GROUP_SEP)
        group_token, _, rest = after.partition(" ")
        group = group_token
        explicit = True
        attrs_text = rest
    else:
        head, _, attrs_text = text.partition(" ")

    sel: str | None = None
    view: str | None = None
    if "::" in head:
        name, _, tail = head.partition("::")
        if "||" in tail:
            sel, _, view = tail.partition("||")
        else:
            sel = tail
        sel = sel or None
        view = view or None
    else:
        name = head
    return name, group, explicit, sel, view, attrs_text


def parse_netlist(body: str) -> list[NetEntry]:
    """Parse the `.NetList` body (entries only, no `.NetList`/`.EndNetList` lines).

    *body* is expected LF-normalized and newline-terminated (that is what
    :attr:`ScanResult.netlist_body` always holds; a stray ``\\r`` is ignored for
    field extraction but preserved in :attr:`NetEntry.raw`). Lines are kept 1:1
    -- including any line that does not look like an entry -- so that
    ``render_netlist(parse_netlist(body), {}) == body``.
    """
    lines = body.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    entries: list[NetEntry] = []
    current_group: str | None = None
    for index, raw in enumerate(lines):
        text = raw[:-1] if raw.endswith("\r") else raw
        if not text.startswith(_ENTRY_PREFIX):
            # Not an entry line (blank line, stray directive): opaque passthrough.
            entries.append(
                NetEntry(
                    name="",
                    group=None,
                    group_explicit=False,
                    sel_state=None,
                    view_mode=None,
                    attrs=(),
                    raw=raw,
                    index=index,
                )
            )
            continue

        name, group, explicit, sel, view, attrs_text = _split_head(text[len(_ENTRY_PREFIX) :])
        if explicit:
            current_group = group
        elif sel is not None or view is not None:
            # spec §2: a classified net drops "::sel||view" -> this line is L1
            # and terminates the enclosing member run.
            group = None
            current_group = None
        else:
            group = current_group

        entries.append(
            NetEntry(
                name=name,
                group=group,
                group_explicit=explicit,
                sel_state=sel,
                view_mode=view,
                attrs=_parse_attrs(attrs_text),
                raw=raw,
                index=index,
            )
        )
    return entries


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _render_entry(e: NetEntry) -> str:
    """Render a single `NetEntry` back to its verbatim (or edited) line text.

    Canonical form per spec §2/§9; ``_render_entry(x) == x.raw`` for every entry
    parsed out of a well-formed body (asserted in `tests/test_netlist.py`).
    """
    if not e.raw.startswith(_ENTRY_PREFIX) and not e.name:
        return e.raw  # opaque passthrough line
    out = [_ENTRY_PREFIX, e.name]
    if e.group_explicit and e.group:
        out.append(f"{_GROUP_SEP}{e.group}")
    if e.sel_state is not None or e.view_mode is not None:
        out.append(f"::{e.sel_state or ''}||{e.view_mode or ''}")
    for key, value in e.attrs:
        out.append(f" {key} = {value}")
    return "".join(out)


def _set_attr(
    attrs: tuple[tuple[str, str], ...], key: str, value: str
) -> tuple[tuple[str, str], ...]:
    """Replace *key* in place if present, else append it (keeps observed order)."""
    for i, (k, _v) in enumerate(attrs):
        if k == key:
            return attrs[:i] + ((key, value),) + attrs[i + 1 :]
    return attrs + ((key, value),)


def _drop_attr(attrs: tuple[tuple[str, str], ...], key: str) -> tuple[tuple[str, str], ...]:
    return tuple((k, v) for k, v in attrs if k != key)


def _fmt_voltage(value: float) -> str:
    return f"{value:g}"


def _desired_group(cfg: PowerNetConfig) -> str | None:
    net_class = (cfg.net_class or "").lower()
    if net_class == "power":
        return _POWER
    if net_class == "ground":
        return _GROUND
    return None


def _editable(e: NetEntry) -> bool:
    """Group nodes, the empty-named root entry and opaque lines are never edited."""
    return bool(e.name) and e.name not in GROUP_NODES and e.raw.startswith(_ENTRY_PREFIX)


def _rewrite(
    e: NetEntry,
    group: str | None,
    first_child: bool,
    cfg: PowerNetConfig | None,
    color: str,
    emit_power_voltage: bool,
) -> NetEntry:
    """Build the edited `NetEntry` for a net whose classification changed."""
    attrs = e.attrs
    if not any(k == "Color" for k, _ in attrs):
        attrs = (("Color", color),) + attrs

    if group == _GROUND:
        attrs = _set_attr(attrs, "Voltage", "0")
    elif group == _POWER and emit_power_voltage and cfg is not None:
        attrs = _set_attr(attrs, "Voltage", _fmt_voltage(cfg.voltage))
    else:
        attrs = _drop_attr(attrs, "Voltage")

    if group is None:
        # spec §9 "net entry, unclassified" template.
        sel: str | None = _SEL_UNSELECTED
        view: str | None = _VIEW_DROPSHAPE
    else:
        sel = view = None  # §A7: classified nets drop "::sel||view"

    return replace(
        e,
        group=group,
        group_explicit=bool(group) and first_child,
        sel_state=sel,
        view_mode=view,
        attrs=attrs,
    )


def _new_entry(name: str) -> NetEntry:
    """A net named only in *configs* (absent from the body) gets a fresh entry.

    It carries no attrs: `_rewrite` assigns the palette colour, so brand-new
    entries and moved entries follow one single colour rule.
    """
    return NetEntry(
        name=name,
        group=None,
        group_explicit=False,
        sel_state=_SEL_UNSELECTED,
        view_mode=_VIEW_DROPSHAPE,
        attrs=(),
        raw="",
        index=-1,
    )


def _sorted_l1_run(entries: list[NetEntry]) -> list[int]:
    """Indices of the leading ASCII-sorted L1 run (design §E group-node insertion).

    Stops at the first L1 entry that breaks ascending order, so the trailing
    out-of-order L1 block (spec §2) cannot drag an insertion point to the end of
    the file.
    """
    run: list[int] = []
    previous = ""
    for i, e in enumerate(entries):
        if e.group is not None or not e.name or not e.raw.startswith(_ENTRY_PREFIX):
            continue
        if run and e.name < previous:
            break
        run.append(i)
        previous = e.name
    return run


def _group_node_index(entries: list[NetEntry], group: str) -> int:
    """Insertion index for a missing *group* node, keeping ASCII sort position."""
    run = _sorted_l1_run(entries)
    for i in run:
        if entries[i].name > group:
            return i
    return (run[-1] + 1) if run else len(entries)


def render_netlist(
    entries: list[NetEntry],
    configs: dict[str, PowerNetConfig],
    *,
    emit_groups: bool = True,
    emit_power_voltage: bool = False,
) -> str:
    """Re-render the netlist body applying *configs*, with minimal edits (design §G.7).

    Every entry whose effective group already equals what *configs* asks for is
    re-emitted from :attr:`NetEntry.raw` verbatim, so the result is
    byte-identical to the parsed body whenever nothing actually changes.
    """
    # ---------------------------------------------------------------- pass 1:
    # decide, per entry, whether it stays put.
    moved: dict[int, str | None] = {}  # index -> desired group
    for i, e in enumerate(entries):
        if not _editable(e):
            continue
        cfg = configs.get(e.name)
        if cfg is None:
            continue
        desired = _desired_group(cfg)
        if desired != e.group:
            moved[i] = desired

    # ---------------------------------------------------------------- pass 2:
    # per group, the surviving member run and what gets appended to it.
    survivors: dict[str, list[int]] = {_POWER: [], _GROUND: []}
    for i, e in enumerate(entries):
        if e.group in survivors and i not in moved:
            survivors[e.group].append(i)

    incoming: dict[str, list[NetEntry]] = {_POWER: [], _GROUND: []}
    declassified: list[NetEntry] = []
    color_seed = len(entries)
    for i, desired in sorted(moved.items()):
        entry = entries[i]
        cfg = configs.get(entry.name)
        color = NETLIST_COLORS[(color_seed + i) % len(NETLIST_COLORS)]
        if desired is None:
            declassified.append(
                _rewrite(entry, None, False, cfg, color, emit_power_voltage)
            )
        else:
            incoming[desired].append(entry)

    # Nets named only in configs and absent from the body.
    known = {e.name for e in entries if e.name}
    for net, cfg in sorted(configs.items()):
        if net in known:
            continue
        desired = _desired_group(cfg)
        if desired is None:
            continue
        incoming[desired].append(_new_entry(net))

    # ---------------------------------------------------------------- pass 3:
    # build the final line list.
    rendered: list[str | None] = [None] * len(entries)
    for i, e in enumerate(entries):
        if i not in moved:
            rendered[i] = e.raw

    # Promote/demote the "-> Group" marker: it belongs on the first member only.
    appended_at: dict[int, list[str]] = {}
    for group in (_GROUND, _POWER):
        run = survivors[group]
        arrivals = sorted(incoming[group], key=lambda e: e.name)
        if not run and not arrivals:
            continue

        # A surviving member only gets rewritten when its "first child" status
        # actually flipped (e.g. the old first child was declassified).
        for position, index in enumerate(run):
            entry = entries[index]
            want_explicit = position == 0
            if entry.group_explicit != want_explicit:
                rendered[index] = _render_entry(
                    replace(entry, group=group, group_explicit=want_explicit)
                )

        lines: list[str] = []
        for position, entry in enumerate(arrivals):
            color = NETLIST_COLORS[(color_seed + position) % len(NETLIST_COLORS)]
            lines.append(
                _render_entry(
                    _rewrite(
                        entry,
                        group,
                        first_child=not run and position == 0,
                        cfg=configs.get(entry.name),
                        color=color,
                        emit_power_voltage=emit_power_voltage,
                    )
                )
            )
        if lines:
            anchor = _append_anchor(entries, survivors, group)
            appended_at.setdefault(anchor, []).extend(lines)

    # ---------------------------------------------------------------- pass 4:
    # missing group nodes, ASCII-sorted among the L1 run.
    group_node_lines: dict[int, list[str]] = {}
    if emit_groups:
        present = {e.name for e in entries if e.name in GROUP_NODES}
        for group in (_GROUND, _POWER):
            # Only a real arrival may add a group node: a pure re-render must
            # never invent a line (that is the byte-identity guarantee).
            if incoming[group] and group not in present:
                index = _group_node_index(entries, group)
                group_node_lines.setdefault(index, []).append(
                    f"{_ENTRY_PREFIX}{group} Color = {GROUP_NODES[group]}"
                )

    out: list[str] = []
    for i in range(len(entries)):
        for line in group_node_lines.get(i, ()):
            out.append(line)
        if rendered[i] is not None:
            out.append(rendered[i])  # type: ignore[arg-type]
        for line in appended_at.get(i, ()):
            out.append(line)
    for line in group_node_lines.get(len(entries), ()):
        out.append(line)
    for line in appended_at.get(len(entries), ()):
        out.append(line)
    for entry in declassified:
        out.append(_render_entry(entry))

    if not out:
        return ""
    return "\n".join(out) + "\n"


def _append_anchor(
    entries: list[NetEntry], survivors: dict[str, list[int]], group: str
) -> int:
    """Index *after* which new members of *group* are appended (design §G.7).

    End of the group's own member run; if the group has no members left, the end
    of the combined member region; if there is no member region at all, the end
    of the body.
    """
    run = survivors[group]
    if run:
        return run[-1]
    combined = [i for indices in survivors.values() for i in indices]
    if combined:
        return max(combined)
    return len(entries)
