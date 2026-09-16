"""
proximity_rules_config.py — Shared placement rules for the two-script pipeline.

Imported by both place_components_organized.py (Script 1, coarse pass) and
place_by_proximity_rules.py (Script 2, fine pass).

Script 1 uses RULES to score IC rotation candidates: it checks whether the
natural face assignment for each satellite at a candidate rotation would be
blocked by a locked component, and penalises blocked rotations.

Script 2 uses RULES, RULE_TYPE_PRIORITY, POWER_NET_EXACT, and is_power_net()
for face-group construction and rotation selection.

route_prep_align.py (Phase 10) uses RULES to validate blocker-clearing moves
and uses preferred_side + group_type_hint to generate ALIGNMENT_GROUPS
automatically from this table.

To adapt for a new project: replace RULES and POWER_NET_EXACT below.
RULE_TYPE_PRIORITY controls face-group placement order and is project-neutral.
"""

# ── Power net classification ──────────────────────────────────────────────────
# Nets listed here are treated as power rails. Used by Script 2's rotation
# heuristic: sym=180 passives on power nets get 180° (positive pad up/left);
# sym=180 passives on signal nets get 0°.
POWER_NET_EXACT = {
    'GND', 'VCC', 'VDD', '+5V', '+3.3V', '+1.8V', '+1.2V',
    'VMAIN', 'VBAT', 'PWR_IN', 'PWR_IN_RAW', 'PWR_IN_FUSED', 'BUCK_SW', 'BUCK_SS',
    'LT_3V3', 'LT_1V8', 'LT_1V2',
    'CM5_3V3', 'USBC_VBUS', 'USBC_VBUS_RAW',
    'STUSB_1V2', 'STUSB_2V7', 'STUSB_VDD',
}

def is_power_net(net_name):
    return bool(net_name) and net_name.upper() in {n.upper() for n in POWER_NET_EXACT}


# ── Face-group placement priority ─────────────────────────────────────────────
# Lower number = placed first. Groups containing higher-priority members are
# resolved before lower-priority groups, so critical components (crystals,
# bootstrap caps) claim space first.
RULE_TYPE_PRIORITY = {
    "crystal":          0,
    "bootstrap":        1,
    "ac_coupling":      2,
    "esd_clamp":        3,
    "decoupling_bypass":4,
    "decoupling_bulk":  5,
    "filter":           6,
    "pullup_pulldown":  7,
    "power_mgmt":       8,
}

# ── Proximity rules ───────────────────────────────────────────────────────────
# Each entry:
#   (ref_a, ref_b, max_dist_mm, rule_type, description, net_hint,
#    preferred_side, group_type_hint)
#
#   ref_a            — satellite component (the one being placed near ref_b)
#   ref_b            — anchor component (already placed or locked)
#   max_dist_mm      — maximum allowed pad-to-pad distance for this pair
#   rule_type        — category string; must be a key in RULE_TYPE_PRIORITY
#   description      — human-readable reason shown in the placement report
#   net_hint         — net name shared by ref_a and ref_b; drives face and
#                      rotation selection. None = proximity-only (no face-group).
#   preferred_side   — which side of ref_b this satellite belongs on for
#                      pre-route alignment. Valid values:
#                        "above"      — above anchor bbox (−Y direction)
#                        "below"      — below anchor bbox (+Y direction)
#                        "left"       — left of anchor bbox (−X direction)
#                        "right"      — right of anchor bbox (+X direction)
#                        "ray_left"   — scan leftward until clear (ray_place)
#                        "ray_right"  — scan rightward until clear (ray_place)
#                        "nearest_pad"— adjacent to the shared net pad; no fixed
#                                       side constraint (script chooses nearest)
#   group_type_hint  — alignment group type for route_prep_align.py. Valid values:
#                        "column"     — single X, stacked vertically
#                        "row"        — single Y, arranged horizontally
#                        "pad_track"  — member X/Y tracks anchor IC pad positions
#                        "ray_place"  — scan outward from anchor bbox edge
#                        "none"       — no alignment group; proximity placement only
#
# Rule-type taxonomy:
#   crystal          — oscillator / crystal components
#   bootstrap        — bootstrap / charge-pump caps
#   ac_coupling      — series AC-coupling caps on signal lines
#   esd_clamp        — TVS / ESD protection diodes
#   decoupling_bypass— local bypass caps and small-value bulk caps
#   decoupling_bulk  — large bulk caps (> ~10µF) or LDO output caps
#   filter           — passive filter networks (inductors, RC networks)
#   pullup_pulldown  — pull-up / pull-down resistors
#   power_mgmt       — power management ICs (ideal diode controllers, etc.)

RULES = [
    # ── TPS54561 buck converter (U1) ──────────────────────────────────────────
    # Switching loop components: placement driven by route_critical.py and
    # Phase 8 placement. No alignment groups — group_type_hint="none".
    ("C_VIN1",        "U1",        15, "decoupling_bulk",   "input bulk cap to TPS54561 VIN",                          "VMAIN",         "nearest_pad", "none"),
    ("C_BOOT1",       "U1",        10, "bootstrap",         "bootstrap cap to TPS54561 BOOT/SW",                       "BUCK_SW",        "nearest_pad", "none"),
    ("C_SS1",         "U1",         8, "decoupling_bypass", "soft-start cap to TPS54561 SS",                           "BUCK_SS",        "nearest_pad", "none"),
    ("C_OUT1",        "L1",        10, "decoupling_bulk",   "output bulk cap at buck +5V output — shares +5V with L1", "+5V",            "nearest_pad", "none"),
    ("L1",            "U1",        12, "filter",            "buck inductor to TPS54561 SW",                            "BUCK_SW",        "nearest_pad", "none"),
    ("R_TOP1",        "U1",        20, "pullup_pulldown",   "feedback top resistor to TPS54561 VSENSE",                "VSENSE_5V",      "nearest_pad", "none"),
    ("R_BOT1",        "U1",        20, "pullup_pulldown",   "feedback bottom resistor to TPS54561 VSENSE",             "VSENSE_5V",      "nearest_pad", "none"),
    ("R_COMP1",       "U1",        12, "filter",            "compensation resistor to TPS54561 COMP",                  "BUCK_COMP",      "nearest_pad", "none"),
    ("C_COMP1",       "R_COMP1",    8, "filter",            "compensation cap at COMP_MID node — shares COMP_MID with R_COMP1", "COMP_MID", "nearest_pad", "none"),

    # ── LT6711A crystal cluster (U3) ─────────────────────────────────────────
    # Y1 scans rightward from U3 bbox edge until clear of all components.
    # XTAL load caps form a column between U3's right bbox edge and Y1.
    ("Y1",            "U3",        12, "crystal",           "27MHz crystal to LT6711A",                                "LT_XTAL_IN",    "right",        "ray_place"),
    ("C_XTAL_IN1",    "U3",        12, "crystal",           "crystal load cap (IN) to LT6711A",                        "LT_XTAL_IN",    "right",        "column"),
    ("C_XTAL_OUT1",   "U3",        12, "crystal",           "crystal load cap (OUT) to LT6711A",                       "LT_XTAL_OUT",   "right",        "column"),

    # ── LT6711A passives — above U3 (TX AC coupling + LT_1V8 bypass) ────────
    # DP TX diff-pair AC coupling caps form a row above U3's top bbox edge,
    # one cap per TX lane half (P and N), ordered L→R matching pad X positions.
    # C_LT1V8_BYP1 sits in a column above the TX cap row — wider courtyard
    # prevents it from fitting in the row at the same Y.
    ("C_TX1P1",       "U3",        10, "ac_coupling",       "DP TX1+ AC cap -- source: LT6711A TX1P output",           "LT_TX1_P",      "above",        "row"),
    ("C_TX1N1",       "U3",        10, "ac_coupling",       "DP TX1- AC cap -- source: LT6711A TX1N output",           "LT_TX1_N",      "above",        "row"),
    ("C_TX2P1",       "U3",        10, "ac_coupling",       "DP TX2+ AC cap -- source: LT6711A TX2P output",           "LT_TX2_P",      "above",        "row"),
    ("C_TX2N1",       "U3",        10, "ac_coupling",       "DP TX2- AC cap -- source: LT6711A TX2N output",           "LT_TX2_N",      "above",        "row"),
    ("C_LT1V8_BYP1",  "U3",         8, "decoupling_bypass", "LT_1V8 bypass cap at LT6711A LT_1V8 input",              "LT_1V8",        "above",        "column"),

    # ── LT6711A passives — right of U3 ───────────────────────────────────────
    # All components in a single column at a fixed X right of U3's right bbox edge.
    # Signal flow top-to-bottom: REXT sense → VBUS detect divider → AUX AC caps.
    ("R_REXT1",       "U3",         8, "decoupling_bypass", "REXT bandgap resistor to LT6711A REXT pin",               "LT_REXT",       "right",        "column"),
    ("R_VDET_H1",     "U3",        12, "filter",            "VBUS detect divider (top) to LT6711A",                    "VBUS_DET",      "right",        "column"),
    ("R_VDET_L1",     "U3",        12, "filter",            "VBUS detect divider (bot) to LT6711A",                    "VBUS_DET",      "right",        "column"),
    ("C_AUX_P1",      "U3",        10, "ac_coupling",       "AUX+ AC cap -- source: LT6711A AUXP pin",                 "LT_AUX_P",      "right",        "column"),
    ("C_AUX_N1",      "U3",        10, "ac_coupling",       "AUX- AC cap -- source: LT6711A AUXN pin",                 "LT_AUX_N",      "right",        "column"),

    # ── LT6711A passives — left of U3 ────────────────────────────────────────
    # HDMI/DP signal pull-ups form a column left of U3, below U8's bottom bbox.
    # Space between U3 left bbox and U8 right bbox is too narrow for a column;
    # the column lives below U8 at the same X (left of U3 centroid).
    ("R_RST1",        "U3",        20, "pullup_pulldown",   "LT_RST_N pull-up to LT6711A",                            "LT_RST_N",      "left",         "column"),
    ("R_HPD1",        "U3",        20, "pullup_pulldown",   "HDMI HPD pull-up to LT6711A",                            "HDMI0_HPD",     "left",         "column"),
    ("R_HDMI_SDA1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SDA pull-up to LT6711A",                        "HDMI0_SDA",     "left",         "column"),
    ("R_HDMI_SCL1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SCL pull-up to LT6711A",                        "HDMI0_SCL",     "left",         "column"),

    # ── LT6711A — SPI flash (IC-to-IC proximity, no alignment group) ─────────
    ("U8",            "U3",        15, "decoupling_bulk",   "SPI flash proximity to LT6711A",                          "LT_SPI_CS",     "nearest_pad", "none"),

    # ── DP TX ESD clamps (J_USB_OUT1) ────────────────────────────────────────
    # ESD clamps placed by Phase 8 proximity rules between U3 and J_USB_OUT1.
    # Placement is signal-path driven; no fixed side or alignment group.
    ("D_TX1",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX1 ESD clamp to USB-C OUT connector",                "DP_TX1_N",      "nearest_pad", "none"),
    ("D_TX2",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX2 ESD clamp to USB-C OUT connector",                "DP_TX2_N",      "nearest_pad", "none"),
    ("D_AUX1",        "J_USB_OUT1", 8, "esd_clamp",         "DP AUX ESD clamp to USB-C OUT connector",                "DP_AUX_N",      "nearest_pad", "none"),

    # ── USB 2.0 ESD clamp (J_USB_IN1) ────────────────────────────────────────
    ("D_USB1",        "J_USB_IN1",  8, "esd_clamp",         "USB D+/D- ESD clamp to USB-C IN connector",              "USB_N",         "nearest_pad", "none"),

    # ── LDO ICs near LT6711A (IC-to-IC proximity, no alignment groups) ───────
    ("U5",            "U3",        25, "decoupling_bulk",   "AP7333-3.3 LDO near LT6711A (LDO-to-load)",              "LT_3V3",        "nearest_pad", "none"),
    ("U6",            "U3",        20, "decoupling_bulk",   "AP7333-1.8 LDO near LT6711A",                            "LT_1V8",        "nearest_pad", "none"),
    ("U7",            "U3",        20, "decoupling_bulk",   "NCP1117-1.2 LDO near LT6711A",                           "LT_1V2",        "nearest_pad", "none"),

    # ── AP7333-3.3 LDO bypass caps (U5) ──────────────────────────────────────
    ("C_LT3V3_BULK1", "U5",         8, "decoupling_bypass", "LT_3V3 bulk cap at AP7333-3.3 output",                   "LT_3V3",        "nearest_pad", "column"),
    ("C_LT3V3_BYP1",  "U5",         8, "decoupling_bypass", "LT_3V3 bypass cap at AP7333-3.3 output",                 "LT_3V3",        "nearest_pad", "column"),
    ("C_U5_IN1",      "U5",         8, "decoupling_bypass", "U5 input bypass at AP7333-3.3 VIN",                      "+5V",           "nearest_pad", "column"),

    # ── AP7333-1.8 LDO bypass cap (U6) ───────────────────────────────────────
    # C_LT1V8_BULK1 scans leftward from U6 to clear the DP TX routing corridor.
    ("C_LT1V8_BULK1", "U6",         8, "decoupling_bypass", "LT_1V8 bulk cap at AP7333-1.8 output",                   "LT_1V8",        "ray_left",    "ray_place"),

    # ── NCP1117-1.2 LDO bypass caps (U7) ─────────────────────────────────────
    ("C_LT1V2_BULK1", "U7",         8, "decoupling_bypass", "LT_1V2 bulk cap at NCP1117-1.2 output",                  "LT_1V2",        "nearest_pad", "column"),
    ("C_LT1V2_BYP1",  "U7",         8, "decoupling_bypass", "LT_1V2 bypass cap at NCP1117-1.2 output",                "LT_1V2",        "nearest_pad", "column"),
    # C_U7_IN1 and C_FLASH_BYP1: U5 occupies the only viable row position between
    # U7, U8, and C_LT1V2_BYP1 in the clean PCB state. Phase 8 placement retained.
    ("C_U7_IN1",      "U7",         8, "decoupling_bypass", "U7 input bypass at NCP1117-1.2 VIN",                     "LT_3V3",        "nearest_pad", "none"),
    ("C_FLASH_BYP1",  "U8",         8, "decoupling_bypass", "SPI flash bypass cap at W25Q32JV VCC",                   "LT_3V3",        "nearest_pad", "none"),

    # ── STUSB4500 bypass caps and I2C pull-ups (U2) ───────────────────────────
    ("C_STUSB_VDD1",  "U2",         8, "decoupling_bypass", "STUSB4500 VDD bypass cap (CM5_3V3)",                     "CM5_3V3",       "nearest_pad", "column"),
    ("C_STUSB_1V2",   "U2",         8, "decoupling_bypass", "STUSB4500 1.2V internal bypass cap",                     "STUSB_1V2",     "nearest_pad", "column"),
    ("C_STUSB_2V7",   "U2",         8, "decoupling_bypass", "STUSB4500 2.7V internal bypass cap",                     "STUSB_2V7",     "nearest_pad", "column"),
    ("C_STUSB_VSYS1", "U2",         8, "decoupling_bypass", "STUSB4500 VSYS bypass cap (USBC_VBUS)",                  "USBC_VBUS",     "nearest_pad", "column"),
    ("R_STSCL1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SCL pull-up to U2",                            "STUSB_SCL",     "nearest_pad", "column"),
    ("R_STSDA1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SDA pull-up to U2",                            "STUSB_SDA",     "nearest_pad", "column"),

    # ── STUSB4500 and ideal diodes near connectors (IC-to-IC, no align groups) ─
    ("U2",            "J_USB_IN1", 25, "power_mgmt",        "STUSB4500 PD sink near USB-C input connector",           "USBC_IN_CC1",   "nearest_pad", "none"),
    ("IC1",           "J_PWR_IN1", 13, "power_mgmt",        "LTC4412 main power ideal diode near power input connector", "PWR_IN",     "nearest_pad", "none"),
    ("IC2",           "J_USB_IN1", 25, "power_mgmt",        "LTC4412 USB ideal diode near USB-C input connector",     "USBC_VBUS",     "nearest_pad", "none"),
    ("Q2",            "IC1",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC1",                 "VMAIN",         "nearest_pad", "none"),
    ("Q3",            "IC2",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC2",                 "USBC_VBUS",     "nearest_pad", "none"),

    # ── Power input protection chain (J_PWR_IN1) ─────────────────────────────
    ("D_TVS",         "J_PWR_IN1", 15, "esd_clamp",         "TVS clamp at power input connector",                     "PWR_IN_RAW",    "nearest_pad", "none"),
    # PWR_IN path: polyfuse → CM choke → Y-caps
    ("F_PWR1",        "J_PWR_IN1", 12, "filter",            "3A polyfuse inline on PWR_IN_RAW at power input connector", "PWR_IN_RAW", "nearest_pad", "none"),
    ("L_CM_PWR1",     "F_PWR1",    12, "filter",            "CM choke after polyfuse — PWR_IN_FUSED → PWR_IN",          "PWR_IN_FUSED","nearest_pad", "none"),
    ("C_CM_PWR_A1",   "L_CM_PWR1",  8, "filter",            "Y-cap at CM choke input (PWR_IN_FUSED → GND)",             "PWR_IN_FUSED","nearest_pad", "none"),
    ("C_CM_PWR_B1",   "L_CM_PWR1",  8, "filter",            "Y-cap at CM choke output (PWR_IN → GND)",                  "PWR_IN",      "nearest_pad", "none"),

    # ── USB-C input protection chain (J_USB_IN1) ─────────────────────────────
    # USBC_VBUS path: CM choke → Y-caps + CC ESD
    ("L_CM_USB1",     "J_USB_IN1", 12, "filter",            "CM choke on USBC_VBUS_RAW at USB-C input connector",       "USBC_VBUS_RAW","nearest_pad", "none"),
    ("C_CM_USB_A1",   "L_CM_USB1",  8, "filter",            "Y-cap at CM choke input (USBC_VBUS_RAW → GND)",            "USBC_VBUS_RAW","nearest_pad", "none"),
    ("C_CM_USB_B1",   "L_CM_USB1",  8, "filter",            "Y-cap at CM choke output (USBC_VBUS → GND)",               "USBC_VBUS",   "nearest_pad", "none"),
    ("D_CC1",         "J_USB_IN1",  8, "esd_clamp",         "ESD clamp on CC1/CC2 lines of USB-C input connector",      "USBC_IN_CC1", "nearest_pad", "none"),

    # ── SOM1 satellites ───────────────────────────────────────────────────────
    ("C_VBAT1",       "SOM1",       8, "decoupling_bypass", "VBAT RTC bypass cap near SOM1 pin 76",                   "CM5_3V3",       "nearest_pad", "none"),
    ("R_BOOT1",       "SOM1",      20, "pullup_pulldown",   "nRPIBOOT pull-up near SOM1",                            "nRPIBOOT",      "nearest_pad", "none"),
    ("J_DEBUG1",      "SOM1",      30, "pullup_pulldown",   "UART debug header near SOM1",                           "GPIO14",        "nearest_pad", "none"),
    ("J_PWR_BTN1",    "SOM1",      40, "pullup_pulldown",   "power button header near SOM1",                         "PWR_BTN",       "nearest_pad", "none"),

    # ── J_DSI1 touch pull-ups ─────────────────────────────────────────────────
    # Touch I2C and reset pull-ups form a row above J_DSI1's top bbox edge.
    ("R_TSDA1",       "J_DSI1",    20, "pullup_pulldown",   "GT911 touch I2C SDA pull-up near J_DSI1",               "TOUCH_SDA",     "above",        "row"),
    ("R_TRST1",       "J_DSI1",    20, "pullup_pulldown",   "touch reset pull-up near DSI connector",                 "TOUCH_RST",     "above",        "row"),
    ("R_TSCL1",       "J_DSI1",    20, "pullup_pulldown",   "touch I2C SCL pull-up near J_DSI1",                     "TOUCH_SCL",     "above",        "row"),
]

# ── Face overrides ────────────────────────────────────────────────────────────
# Forces a specific world-direction face for (satellite, anchor) pairs where the
# natural pin-based assignment conflicts with board obstacles or layout intent.
# "left"=−x  "right"=+x  "up"=−y  "down"=+y  (world coordinates)
#
# Used by Script 2 to assign face groups. Used by Script 1 to determine which
# side of a locked satellite an unlocked anchor IC should be seeded on.

# ── Anchor offset overrides ───────────────────────────────────────────────────
# Overrides ANCHOR_OFFSET_FACTOR for specific root anchors. Use when the default
# factor places the anchor outside a physically constrained valid range.
# U1: valid x-range between L1 right edge (150.155) and J_USB_IN1 left edge (156.403)
# is only 0.46mm wide (U1 hw=2.745, gap 0.15mm). Factor 0.535×12=6.42mm → U1.x=153.28.
ANCHOR_OFFSET_OVERRIDES = {
    "U1": 0.535,
}

# ── Normalization exclusions ──────────────────────────────────────────────────
# Refs excluded from the column/row normalization pass in Script 2.
# (Script 2 also auto-excludes any ref that is itself an anchor in RULES.)
# Q3: anchor_clearance_pass pushes Q3 slightly; col-norm oscillates it back each pass.
# C_FLASH_BYP1: col-norm anchor-edge formula ~0.05mm short of U8 courtyard edge,
#   leaving only 0.1mm gap (need 0.15mm). Face-group places it correctly; skip col-norm.
# D_USB1, C_COMP1: col-norm snaps them into neighboring IC courtyards.
NO_NORMALIZE_REFS = {"D_TVS", "Q3", "C_FLASH_BYP1", "D_USB1", "C_COMP1"}

# ── No-force refs ─────────────────────────────────────────────────────────────
# Components excluded from overlap resolution movement in Script 2.
# Prefixes: test points (TP_) and mounting holes (MH_) are never pushed.
# Specific refs: monitor mounting holes that are close to board edge.
NO_FORCE_PREFIXES = ("TP_", "MH_")
NO_FORCE_REFS     = {"MH_MONITOR_TL", "MH_MONITOR_TR"}
