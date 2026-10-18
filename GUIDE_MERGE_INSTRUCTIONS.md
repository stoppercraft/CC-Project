# Guide Merge Instructions
# Apply when combining Phase10_Alignment_Guide_Addition.md and
# Routing_Phase10_SessionGuide.md into PCB_Design_Guide_Generic.md.

---

## EDIT 1 — Replace In4.Cu zone fill with trace routing description in Phase 9

**File:** `PCB_Design_Guide_Generic.md`
**Locate:** The `## PHASE 9 — Copper Zones` section. Find the bullet:

```
- **In4.Cu POWER PLANES** — separate polygons for each major rail (`[POWER_RAIL_MAIN]`
  over Zone B, `[POWER_RAIL_5V]` over remainder, `[POWER_RAIL_LOW_V]` island near
  loads). 0.5 mm gap between adjacent power zones.
```

**Replace it with:**

```
- **In4.Cu POWER LAYER** — designated trace-routing layer for all switching converter
  power rails (`[POWER_RAIL_MAIN]`, `[POWER_RAIL_5V]`, and any other converter outputs).
  No zone fills on this layer. Power distribution is handled entirely by `route_critical.py`
  trace routing in Phase 10 Part A. All components are SMD on F.Cu; through-hole fanout
  vias already span all layers including In4.Cu, so power trace routing on In4.Cu costs
  no additional vias over routing on B.Cu. In4.Cu is sandwiched between In3.Cu (GND) and
  In1.Cu (GND), giving power traces a shielded return path on both sides. Leave In4.Cu
  unfilled (no zone) — route_critical.py places all copper on this layer. Record
  `"In4.Cu"` in `routing_config.py::LAYER_SCHEME["power"]`.
```

---

## EDIT 2 — Replace "same power zone sufficient" in proximity rule descriptions

Power rails are distributed by route_critical.py's full-net trace routing pass, not by
copper fill zones.

**File:** `PCB_Design_Guide_Generic.md`
**Find all occurrences of:** `same power zone sufficient`
**Replace each with:** `same power rail trace sufficient`

There are two occurrences. Both are in the `decoupling_bulk` row of proximity rule
threshold tables.

---

## EDIT 3 — Same replacement in Phase10_Alignment_Guide_Addition.md content

**File:** Content merged from `Phase10_Alignment_Guide_Addition.md` (Section 1 —
Phase 2 Sub-step B2 replacement), the threshold lookup table row:

```
| `decoupling_bulk` | 15 | Bulk cap — less loop-critical, same power zone sufficient |
```

**Change the description to:** `Bulk cap — less loop-critical, same power rail trace sufficient`

---

## EDIT 4 — Add full-net routing documentation to Phase 10 Part A

**File:** Content merged from `Phase10_Alignment_Guide_Addition.md` (Section 4 —
Phase 10 Part A replacement). Find the "What the script routes per loop" table:

| Connection | Net | Layer | Notes |
|---|---|---|---|
| `ic_ref` → `inductor_ref` | `sw_net` | `layer` | Switching node — minimize length and area |
| `inductor_ref` → each `output_caps` | `out_net` | `layer` | Output rail |
| `ic_ref` → each `input_caps` | `vin_net` | `layer` | Input rail |
| `ic_ref` → `bootstrap_cap` | `bootstrap_net` | `layer` | Control signal — narrow trace |
| GND via beside each `output_caps` | `gnd_net` | `layer` | Short stub + via; via sized from `CLEARANCE_AUDIT` |
| GND via beside each `input_caps` | `gnd_net` | `layer` | Same |

**Add the following paragraph immediately after that table:**

> **Full-net distribution pass:** After routing the tight switching loop connections above,
> `route_critical.py` runs a second pass over every net in the loop using Prim's MST to
> connect all remaining pads on that net. This distributes each power rail to every consumer
> (LDO inputs, SOM power pins, test points, sense resistors) via routed traces on the same
> layer as the switching loop (`LAYER_SCHEME["power"]`, typically In4.Cu). Fanout vias
> placed by Part 0.75 are through-hole and reach In4.Cu directly — no additional transition
> vias are needed. Tap refs listed in `full_net_taps` (sense resistors, test points) connect
> via narrow traces off the main trunk at their effective pad width; all other consumers
> connect at full trunk width.

**Also add `full_net_taps` to the SWITCHING_LOOPS config template** in Step A1. After the
`"boot_via_allowed"` key, add:

```python
        # Tap refs: refs on this net that carry only sense/measurement current.
        # They connect via narrow traces off the main spine at their effective pad
        # width rather than at full trunk width. All other consumers on each net
        # connect at full trunk width.
        # Omit or set to [] if all consumers on this net carry full load current.
        "full_net_taps":      [],   # e.g. ["R_VDET_H1", "TP_5V1"] for sense + test point
```

**Also update the `"layer"` field comment** in the SWITCHING_LOOPS template from:

```python
        # Routing layer for all traces in this loop (typically F.Cu for a buck converter)
        "layer":              "F.Cu",
```

to:

```python
        # Routing layer for all traces in this loop. Use LAYER_SCHEME["power"] from
        # routing_config.py — typically In4.Cu when all components are SMD on F.Cu and
        # through-hole fanout vias already reach the inner layer.
        "layer":              "In4.Cu",
```

---

## EDIT 5 — Update routing_config.py LAYER_SCHEME comment for In4.Cu

**File:** `routing_config.py` (already done — change was applied directly when this
instruction set was written). Verify the file reads:

```python
# Stackup (6-layer):
#   In4.Cu — power trace routing (+5V / VMAIN and all switching converter rails)
# ...
LAYER_SCHEME = {
    "power":     "In4.Cu",
    ...
}
```

And SWITCHING_LOOPS reads `"layer": "In4.Cu"`. If these are already in place, no further
action needed for this edit.

---

## EDIT 6 — Update PRE-ROUTING GATE zone checklist item

**File:** `PCB_Design_Guide_Generic.md`
**Locate:** The `## PRE-ROUTING GATE` section checklist line beginning:
```
- [ ] All zones present per PROJECT_PARAMS stackup (GND planes on correct layers, power
```
**Replace with:**
```
- [ ] All GND zones present: GND planes on In1.Cu and In3.Cu, GND fill on F.Cu and B.Cu,
      local EP islands. In4.Cu left unfilled — power traces placed by route_critical.py
      in Phase 10 Part A.
```

---

## EDIT 7 — Update Phase 9 SESSION_CONTEXT note

**File:** `PCB_Design_Guide_Generic.md`
**Locate:** End of Phase 9:
```
**SESSION_CONTEXT:** Phase 9 complete, zone list.
```
**Change to:**
```
**SESSION_CONTEXT:** Phase 9 complete. GND zones filled on In1.Cu, In3.Cu, F.Cu, B.Cu.
In4.Cu left unfilled (power traces placed by Phase 10 Part A).
```

---

## Summary of what changes and why

In4.Cu is the power trace routing layer. All components are SMD on F.Cu. Through-hole
fanout vias already span all layers including In4.Cu, so routing power traces on In4.Cu
costs no additional vias vs. routing on B.Cu. In4.Cu is sandwiched between two solid GND
planes (In3.Cu and In1.Cu), giving power traces a shielded return path on both sides and
keeping B.Cu free for CM5 fanout and slow signals.

Zone fills for power rails are removed entirely. Power distribution is handled by
`route_critical.py`'s full-net MST trace routing pass in Phase 10 Part A.
