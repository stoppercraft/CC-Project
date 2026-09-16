"""
routing_config.py — Project-specific Phase 10 routing data for Frameline_Compute_V2.

Imported by:
  route_prep_align.py    — ALIGNMENT_GROUPS, ROTATION_SYMMETRY
  route_critical.py      — SWITCHING_LOOPS, PCB_FILE, KICAD_SITE_PKGS
  route_highspeed.py     — HS_PAIRS, PCB_FILE, KICAD_SITE_PKGS
  verify_highspeed.py    — HS_PAIRS, LANE_GROUPS, PCB_FILE, REPORTS_DIR
  lock_highspeed_nets.py — HS_NETS, PCB_FILE, KICAD_SITE_PKGS
  run_freerouting.py     — FREEROUTING_JAR, FREEROUTING_TEMP_DIR, PCB_FILE

To adapt for a new project: replace every value in this file.
The scripts themselves contain zero hardcoded refs, net names, or layer names.
"""

# ── Paths ─────────────────────────────────────────────────────────────────────
KICAD_SITE_PKGS      = r"C:\Program Files\KiCad\10.0\bin\Lib\site-packages"
PCB_FILE             = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
REPORTS_DIR          = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Reports"
FREEROUTING_JAR      = r"C:/Temp/freerouting/freerouting-executable.jar"
FREEROUTING_TEMP_DIR = r"C:/Temp/freerouting"

# ── Component rotation symmetry ───────────────────────────────────────────────
# Used by route_prep_align.py to determine which rotations it may apply.
# "none"  — never rotate; pad arrangement is electrically asymmetric at any other angle.
# "180"   — 0° and 180° are equivalent; no other angle is valid.
# "4fold" — 0°, 90°, 180°, 270° are all equivalent.
# Locked components are omitted — they are never touched regardless of symmetry.
ROTATION_SYMMETRY = {
    # Capacitors — 0402 two-pad symmetric
    "C_AUX_N1":      "180",
    "C_AUX_P1":      "180",
    "C_BOOT1":       "180",
    "C_CM_PWR_A1":   "180",
    "C_CM_PWR_B1":   "180",
    "C_CM_USB_A1":   "180",
    "C_CM_USB_B1":   "180",
    "C_COMP1":       "180",
    "C_FLASH_BYP1":  "180",
    "C_LT1V2_BULK1": "180",
    "C_LT1V2_BYP1":  "180",
    "C_LT1V8_BULK1": "180",
    "C_LT1V8_BYP1":  "180",
    "C_LT3V3_BULK1": "180",
    "C_LT3V3_BYP1":  "180",
    "C_OUT1":        "180",
    "C_SS1":         "180",
    "C_STUSB_1V2":   "180",
    "C_STUSB_2V7":   "180",
    "C_STUSB_VDD1":  "180",
    "C_STUSB_VSYS1": "180",
    "C_TX1N1":       "180",
    "C_TX1P1":       "180",
    "C_TX2N1":       "180",
    "C_TX2P1":       "180",
    "C_U5_IN1":      "180",
    "C_U7_IN1":      "180",
    "C_VBAT1":       "180",
    "C_VIN1":        "180",
    "C_XTAL_IN1":    "180",
    "C_XTAL_OUT1":   "180",
    # Resistors — 0402 two-pad symmetric
    "R_BOOT1":       "180",
    "R_BOT1":        "180",
    "R_COMP1":       "180",
    "R_HDMI_SCL1":   "180",
    "R_HDMI_SDA1":   "180",
    "R_HPD1":        "180",
    "R_REXT1":       "180",
    "R_RST1":        "180",
    "R_STSCL1":      "180",
    "R_STSDA1":      "180",
    "R_TOP1":        "180",
    "R_TRST1":       "180",
    "R_TSCL1":       "180",
    "R_TSDA1":       "180",
    "R_VDET_H1":     "180",
    "R_VDET_L1":     "180",
    # Protection / power path
    "D_AUX1":        "180",
    "D_CC1":         "180",
    "D_TVS":         "180",
    "D_TX1":         "180",
    "D_TX2":         "180",
    "D_USB1":        "180",
    "F_PWR1":        "180",
    "IC1":           "180",
    "IC2":           "180",
    "L_CM_PWR1":     "180",
    "L_CM_USB1":     "180",
    "U1":            "180",
    "U8":            "180",
    "Y1":            "180",
    # Asymmetric 3-terminal devices — never rotate
    "Q2":            "none",
    "Q3":            "none",
    "U5":            "none",
    "U6":            "none",
}

# ── High-speed differential pairs ─────────────────────────────────────────────
# name -> (P_net, N_net, routing_layer, intra_pair_skew_fail_mm)
#
# NOTE: verify_highspeed.py accepts a single HS_LAYER_NAME per invocation.
# Run once with "In2.Cu" for HDMI0/DP Alt Mode/MIPI1; once with "F.Cu" for USB2.
# lock_highspeed_nets.py uses HS_NETS (flat list below) and is layer-agnostic.
HS_PAIRS = {
    # HDMI0: CM5 SOM2 → LT6711A (U3) — In2.Cu buried stripline, 100Ω diff
    "HDMI0_CLK": ("HDMI0_CLK_P", "HDMI0_CLK_N", "In2.Cu", 0.127),
    "HDMI0_TX0": ("HDMI0_TX0_P", "HDMI0_TX0_N", "In2.Cu", 0.127),
    "HDMI0_TX1": ("HDMI0_TX1_P", "HDMI0_TX1_N", "In2.Cu", 0.127),
    "HDMI0_TX2": ("HDMI0_TX2_P", "HDMI0_TX2_N", "In2.Cu", 0.127),
    # DP TX source: LT6711A (U3) → AC coupling caps — In2.Cu buried stripline, 100Ω diff
    "LT_TX1":    ("LT_TX1_P",    "LT_TX1_N",    "In2.Cu", 0.127),
    "LT_TX2":    ("LT_TX2_P",    "LT_TX2_N",    "In2.Cu", 0.127),
    "LT_AUX":    ("LT_AUX_P",    "LT_AUX_N",    "In2.Cu", 0.127),
    # DP TX connector: ESD (D_TX1/D_TX2/D_AUX1) → J_USB_OUT1 — In2.Cu, 100Ω diff
    "DP_TX1":    ("DP_TX1_P",    "DP_TX1_N",    "In2.Cu", 0.127),
    "DP_TX2":    ("DP_TX2_P",    "DP_TX2_N",    "In2.Cu", 0.127),
    "DP_AUX":    ("DP_AUX_P",    "DP_AUX_N",    "In2.Cu", 0.127),
    # MIPI1: CM5 SOM2 → J_DSI1 — In2.Cu buried stripline, 100Ω diff (D2/D3 lanes NC)
    "MIPI1_CLK": ("MIPI1_C_P",   "MIPI1_C_N",   "In2.Cu", 0.127),
    "MIPI1_D0":  ("MIPI1_D0_P",  "MIPI1_D0_N",  "In2.Cu", 0.127),
    "MIPI1_D1":  ("MIPI1_D1_P",  "MIPI1_D1_N",  "In2.Cu", 0.127),
    # USB 2.0: J_USB_IN1 → D_USB1 → CM5 SOM2 — F.Cu, 90Ω diff
    "USB2":      ("USB_P",       "USB_N",        "F.Cu",   0.500),
}

# Flat net list for lock_highspeed_nets.py
HS_NETS = sorted({net for p, n, _l, _s in HS_PAIRS.values() for net in (p, n)})

# Inter-lane spread groups for verify_highspeed.py
# (pair_name_list, max_spread_mm)
LANE_GROUPS = {
    "HDMI0 lanes (limit 0.50mm)":        (["HDMI0_CLK", "HDMI0_TX0", "HDMI0_TX1", "HDMI0_TX2"], 0.50),
    "DP TX source lanes (limit 0.45mm)":  (["LT_TX1",    "LT_TX2",    "LT_AUX"],                  0.45),
    "DP TX conn lanes (limit 0.45mm)":    (["DP_TX1",    "DP_TX2",    "DP_AUX"],                   0.45),
    "MIPI1 active lanes (limit 0.50mm)":  (["MIPI1_CLK", "MIPI1_D0",  "MIPI1_D1"],                0.50),
}

# Routing widths and gap per diff pair — used by route_highspeed.py
# name -> (width_mm, gap_mm)  — both in mm, determines edge-to-edge copper gap
HS_ROUTE_WIDTHS = {
    # HDMI0 and LT6711A outputs — 100Ω diff, fine pitch
    "HDMI0_CLK": (0.127, 0.10),
    "HDMI0_TX0": (0.127, 0.10),
    "HDMI0_TX1": (0.127, 0.10),
    "HDMI0_TX2": (0.127, 0.10),
    "LT_TX1":    (0.127, 0.10),
    "LT_TX2":    (0.127, 0.10),
    "LT_AUX":    (0.127, 0.10),
    # DP connector — 100Ω diff, standard pitch
    "DP_TX1":    (0.150, 0.15),
    "DP_TX2":    (0.150, 0.15),
    "DP_AUX":    (0.127, 0.10),
    # MIPI1 — In2.Cu buried stripline, 100Ω diff
    "MIPI1_CLK": (0.127, 0.10),
    "MIPI1_D0":  (0.127, 0.10),
    "MIPI1_D1":  (0.127, 0.10),
    # USB 2.0 — F.Cu microstrip, 90Ω diff
    "USB2":      (0.20, 0.15),
}

# ── Layer scheme ─────────────────────────────────────────────────────────────
# Maps signal categories to their designated PCB layers.
# Stackup (6-layer):
#   F.Cu   — escape routing + slow signals (I2C, GPIO, SPI, debug)
#   In1.Cu — GND plane (F.Cu reference / In2.Cu top reference)
#   In2.Cu — HS diff pairs, buried stripline (HDMI0, DP Alt Mode, MIPI1)
#   In3.Cu — GND plane (In2.Cu bottom reference)
#   In4.Cu — power planes (+5V / VMAIN)
#   B.Cu   — CM5 fanout + slow signals + power switching
# power_alt: layer used when a power trace cannot route on the primary layer.
LAYER_SCHEME = {
    "power":     "B.Cu",   # switching loop power traces (primary layer)
    "power_alt": "F.Cu",   # via-detour escape layer for blocked power traces
    "highspeed": "In2.Cu", # HS differential pairs (buried stripline)
    "gnd_ref":   "GND",    # GND reference net name for return via pairing
}

# ── Fanout via skip nets ──────────────────────────────────────────────────────
# Nets listed here are excluded from fanout via generation entirely.
# Use for fill-connected nets (GND, power planes) that are stitched by copper
# pours rather than per-pad vias. Works on any project — list whatever
# fill-connected net names should be skipped.
FANOUT_VIA_SKIP_NETS = [
    "GND",
]

# ── Routing layer priority ────────────────────────────────────────────────────
# Ordered list of routable copper layers, most-preferred first.
# Used by route_fanout_vias.py to assign fanout vias to nets whose routing
# layer is not explicitly declared in HS_PAIRS or SWITCHING_LOOPS.
# Plane layers (In1.Cu, In3.Cu, In4.Cu) are excluded — they are power/GND
# fills and are never used for signal routing.
ROUTING_LAYER_PRIORITY = [
    "F.Cu",    # primary signal layer — escape routing and slow signals
    "B.Cu",    # secondary signal layer — CM5 fanout and slow signals
    "In2.Cu",  # HS buried stripline — used only for nets in HS_PAIRS
]

# ── Routing clearance audit — for route_clearance_audit.py ───────────────────
CLEARANCE_AUDIT = {
    "signal_trace_width_mm":   0.20,   # default signal trace width for unlisted nets
    "via_drill_mm":            0.30,   # standard signal/power via drill diameter
    "via_annular_ring_mm":     0.15,   # standard via copper annular ring width
    "via_annular_ring_min_mm": 0.10,  # minimum annular ring for via-in-pad fit (PCBWay standard floor)
    "via_clearance_mm":        0.15,   # via copper-to-copper clearance
    "hs_via_drill_mm":         0.30,   # HS differential pair layer-change via drill
    "hs_via_annular_ring_mm":  0.15,   # HS via annular ring width
    "neckdown_stub_w_mm":      0.10,   # escape stub width for fine-pitch neckdown threading (PCBWay standard min)
    "neckdown_length_mm":      0.50,   # minimum neckdown transition zone length beside pad
    "fanout_corridor_len_mm":  3.0,    # how far ahead to reserve escape corridors for non-via pads
    "diff_pair_min_sep_mm":    0.0,    # minimum edge-to-edge gap between diff-pair components
    "board_margin_mm":         3.0,    # minimum distance from board edge for any component
    "corridor_margin_mm":      0.50,   # extra routing margin added to calculated corridor minimum
    "board_edge_exclude_prefixes": ["MH", "J", "P", "SOM", "CN", "TP"],
                                        # reference prefixes that may intentionally sit at the board
                                        # edge (mounting holes, connectors) — excluded from board
                                        # edge margin check
    "type_gap_exclude_prefixes":  ["MH"],
                                        # reference prefixes excluded from component-type gap check
                                        # (mounting holes have oversized courtyards by design —
                                        # nearby SMT components do not create routing conflicts)
    "type_gap_passive_mm":   0.025,     # minimum edge-to-edge gap between two passives
    "type_gap_ic_mm":        0.150,     # minimum gap when either component is an IC
    "type_gap_connector_mm": 0.500,     # minimum gap when either component is a connector
    "type_gap_default_mm":   0.100,     # fallback for unclassified component types
    "via_keepout_exclude_refs": [
        "C_TX1N1", "C_TX1P1", "C_TX2N1", "C_TX2P1",
                                        # DP/HDMI AC coupling caps — sandwiched between SOM1 and
                                        # U3 with no pad-side via room; vias land along the route
        "C_AUX_P1", "C_AUX_N1",
                                        # AUX diff pair AC coupling caps — same situation; no room
                                        # for pad-side vias, via lands along the AUX route segment
    ],
    "hs_path_exclude_refs": [
        "C_TX1N1", "C_TX1P1", "C_TX2N1", "C_TX2P1",
                                        # AC coupling caps intentionally in the DP/USB HS path
                                        # envelopes — they are part of the signal path, not blockers
    ],
    "type_gap_exclude_refs": [
        "C_TX1N1", "C_TX1P1", "C_TX2N1", "C_TX2P1",
                                        # intra-pair gaps set by proximity rules; courtyard-to-
                                        # courtyard spacing is intentionally tighter than passive min
        "C_LT1V8_BULK1",
                                        # intentionally close to U6 by design (skip_corridor pair)
    ],
}

# ── Switching loops for route_critical.py ────────────────────────────────────
SWITCHING_LOOPS = [
    {
        "name":               "TPS54561 buck — U1",
        "layer":              "B.Cu",
        "sw_width_mm":        2.0,   # switching node (3–5A peak)
        "out_width_mm":       2.5,   # output rail (5A)
        "vin_width_mm":       2.0,   # input rail (3A)
        "bootstrap_width_mm": 0.3,   # bootstrap / control (< 0.5A)
        "ic_ref":             "U1",
        "inductor_ref":       "L1",
        "sw_net":             "BUCK_SW",
        "vin_net":            "VMAIN",
        "out_net":            "+5V",
        "gnd_net":            "GND",
        "input_caps":         ["C_VIN1"],
        "output_caps":        ["C_OUT1"],
        "bootstrap_cap":      "C_BOOT1",
        "bootstrap_net":      "BUCK_BOOT",
        "neckdown_width_mm":  1.0,   # stub width when full-width exit from IC pad is blocked
        "neckdown_max_mm":    15.0,  # max stub length before giving up
        # bootstrap_layer: route the bootstrap trace on the IC's own layer (F.Cu) — both U1
        # and C_BOOT1 are SMD on F.Cu and the trace is short/narrow (0.3mm). Keeping it on
        # F.Cu avoids needing vias and prevents the BOOT route from blocking B.Cu power paths.
        "bootstrap_layer":  "F.Cu",
        # Via-detour: when direct routing is blocked, attempt layer change via LAYER_SCHEME[power_alt].
        # SW: via-detour enabled — C_BOOT1.1 physically traps a 2mm trace at U1.SW; B.Cu detour
        # keeps loop area acceptable (<50mm²). BOOT: False — bootstrap cap must be directly at IC.
        "sw_via_allowed":   True,
        "vin_via_allowed":  True,
        "out_via_allowed":  True,
        "boot_via_allowed": False,
    },
]

# ── Alignment groups for route_prep_align.py ─────────────────────────────────
#
# Group type "pad_track":
#   The anchor is a large IC. Each member's position on track_axis is taken
#   directly from a named pad on that anchor IC. All members share the same
#   coordinate on fixed_axis, computed as:
#       target_fixed = mean(anchor_pad[fixed_axis] for all members) + fixed_offset_mm
#   Positive fixed_offset_mm = increasing coordinate (rightward for X, downward for Y).
#   target_rotation is applied to each member whose ROTATION_SYMMETRY != "none".
#   Locked members are skipped silently.
#
#   members: list of {"ref": <ref>, "anchor_pad_net": <net_name>}
#     Each member's track_axis coordinate = the pad on anchor_ref that carries
#     anchor_pad_net. If the anchor has multiple pads on that net, the script
#     uses the one geometrically closest to the member's current position.
#
# Group type "column":
#   All members share the same X coordinate (anchor centroid X + x_offset_mm).
#   Members are placed in order at Y = anchor_centroid_Y + y_start_mm,
#   then anchor_centroid_Y + y_start_mm + pitch_mm, etc.
#   target_rotation is applied to all members whose ROTATION_SYMMETRY != "none".
#   Locked members are skipped silently (the next unlocked member fills that slot).
#
# Group type "lift":
#   Each member moves to Y = anchor_centroid_Y + y_offset_mm while its current
#   X coordinate is preserved exactly (no pitch, no x_start).  Use when a set of
#   components should shift vertically as a unit without disturbing the relative
#   horizontal spacings set by the proximity-rules placement pass.
#
# Group type "push":
#   Rigid-body shift of all components on one side of the anchor's bbox.
#   members = "auto" (the only supported value) — auto-detects every unlocked
#   footprint whose centroid lies beyond the anchor bbox edge in 'direction'.
#   Computes the corridor deficit and shifts ALL members by the same (dx, dy),
#   preserving every relative position and rotation exactly.  No absolute
#   target coordinates; only the minimum outward shift to meet corridor clearance.

ALIGNMENT_GROUPS = [
    {
        "name":       "U3 right cluster — rigid push",
        "type":       "push",
        "anchor_ref": "U3",
        "direction":  "right",
        "members": [
            "Y1",
            "C_XTAL_IN1", "C_XTAL_OUT1",
            "R_REXT1", "R_VDET_H1", "R_VDET_L1",
            "C_AUX_P1",  "C_AUX_N1",
            # U2 (STUSB4500) and its immediate satellites move with the cluster
            # so Y1 is not blocked by U2 at the proposed +1.71mm position.
            "U2",
            "C_STUSB_VDD1", "C_STUSB_VSYS1", "C_STUSB_1V2", "C_STUSB_2V7",
            "R_STSCL1", "R_STSDA1",
        ],
    },
    {
        "name":            "U3 above lift — TX caps and BYP1",
        "type":            "lift",
        "anchor_ref":      "U3",
        "y_offset_mm":     -8.0,
        "members":         ["C_TX1N1", "C_TX1P1", "C_TX2N1", "C_TX2P1", "C_LT1V8_BYP1"],
    },
    {
        "name":            "U3 left column — signal resistors",
        "type":            "column",
        "anchor_ref":      "U3",
        "x_offset_mm":     -6.900,
        "y_start_mm":      -0.200,
        "target_rotation":   0.0,
        "first_pitch_mm":  1.44,      # wider first gap: 0.5mm courtyard clearance for trace routing
        "pitch_mm":        1.016,
        "skip_check_a":    True,      # pitch below clearance threshold — preserve exactly
        "skip_pad_gap":    True,      # members connect directly to anchor pads
        "members":         ["R_RST1", "R_HPD1", "R_HDMI_SCL1", "R_HDMI_SDA1"],
    },
    {
        "name":       "U3 left cluster — rigid push",
        "type":       "push",
        "anchor_ref": "U3",
        "direction":  "left",
        # corridor_occupants: components that physically sit in the routing corridor
        # between U3 and this cluster. The push gap is widened so the cluster also
        # clears their bodies (with CLEARANCE_MM gap), not just the trace corridor.
        "corridor_occupants": ["R_RST1", "R_HPD1", "R_HDMI_SCL1", "R_HDMI_SDA1"],
        "members": [
            "U5",  "C_U5_IN1",  "C_LT3V3_BYP1",  "C_LT3V3_BULK1",
            "U7",  "C_U7_IN1",  "C_LT1V2_BYP1",  "C_LT1V2_BULK1",
            "U8",  "C_FLASH_BYP1",
        ],
    },
    {
        "name":            "U6 shift — left to clear TX caps row",
        "type":            "column",
        "anchor_ref":      "U3",
        "x_offset_mm":     -5.996,
        "y_start_mm":      -10.471,
        "pitch_mm":        1.65,
        "target_rotation": 0.0,
        "members":         ["U6"],
    },
    {
        "name":            "U6 right offset — C_LT1V8_BULK1",
        "type":            "column",
        "anchor_ref":      "U6",
        "x_offset_mm":     2.779,
        "y_start_mm":      0.241,
        "pitch_mm":        1.65,
        "skip_corridor":   True,
        "members":         ["C_LT1V8_BULK1"],
    },
    {
        "name":            "J_DSI1 above row — auto-generated",
        "type":            "row",
        "anchor_ref":      "J_DSI1",
        "y_offset_mm":     -5.5725,
        "x_start_mm":      -1.2,
        "pitch_mm":        2.2,
        "target_rotation": 0.0,
        "members":         ["R_TRST1", "R_TSDA1", "R_TSCL1"],
    },
]
