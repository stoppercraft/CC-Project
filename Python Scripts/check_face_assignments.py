"""
DEV TOOL — not part of production script or final guide.

Compares the face assignment that place_by_proximity_rules.py would compute for
each RULE pair against the face the designer actually used in the clean backup.

Run this any time placement behaviour seems wrong for a rule pair. A MISMATCH
row means the script will place the satellite on the wrong side of the anchor.

Usage:
    python check_face_assignments.py
"""

import json, math, os

# ── PATHS ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
GEOM_DB_PATH = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\component_geometry.json"
BACKUP_POS   = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Reports\clean_backup_positions.txt"

# ── RULES (copied verbatim from place_by_proximity_rules.py) ─────────────────
RULES = [
    ("C_VIN1",        "U1",        15, "decoupling_bulk",   "input bulk cap to TPS54561 VIN",                          "VMAIN"),
    ("C_BOOT1",       "U1",         8, "bootstrap",         "bootstrap cap to TPS54561 BOOT/SW",                       "BUCK_SW"),
    ("C_SS1",         "U1",         8, "decoupling_bypass", "soft-start cap to TPS54561 SS",                           "BUCK_SS"),
    ("C_OUT1",        "L1",        10, "decoupling_bulk",   "output bulk cap at buck +5V output — shares +5V with L1", "+5V"),
    ("L1",            "U1",        12, "filter",            "buck inductor to TPS54561 SW",                            "BUCK_SW"),
    ("R_TOP1",        "U1",        20, "pullup_pulldown",   "feedback top resistor to TPS54561 VSENSE",                "VSENSE_5V"),
    ("R_BOT1",        "U1",        20, "pullup_pulldown",   "feedback bottom resistor to TPS54561 VSENSE",             "VSENSE_5V"),
    ("R_COMP1",       "U1",        12, "filter",            "compensation resistor to TPS54561 COMP",                  "BUCK_COMP"),
    ("C_COMP1",       "R_COMP1",    8, "filter",            "compensation cap at COMP_MID node",                       "COMP_MID"),
    ("Y1",            "U3",        12, "crystal",           "27MHz crystal to LT6711A",                                "LT_XTAL_IN"),
    ("C_XTAL_IN1",    "U3",        12, "crystal",           "crystal load cap (IN) to LT6711A",                        "LT_XTAL_IN"),
    ("C_XTAL_OUT1",   "U3",        12, "crystal",           "crystal load cap (OUT) to LT6711A",                       "LT_XTAL_OUT"),
    ("R_REXT1",       "U3",         8, "decoupling_bypass", "REXT bandgap resistor to LT6711A REXT pin",               "LT_REXT"),
    ("R_RST1",        "U3",        20, "pullup_pulldown",   "LT_RST_N pull-up to LT6711A",                            "LT_RST_N"),
    ("R_HPD1",        "U3",        20, "pullup_pulldown",   "HDMI HPD pull-up to LT6711A",                            "HDMI0_HPD"),
    ("R_HDMI_SDA1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SDA pull-up to LT6711A",                        "HDMI0_SDA"),
    ("R_HDMI_SCL1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SCL pull-up to LT6711A",                        "HDMI0_SCL"),
    ("R_VDET_H1",     "U3",        12, "filter",            "VBUS detect divider (top) to LT6711A",                   "VBUS_DET"),
    ("R_VDET_L1",     "U3",        12, "filter",            "VBUS detect divider (bot) to LT6711A",                   "VBUS_DET"),
    ("U8",            "U3",        15, "decoupling_bulk",   "SPI flash proximity to LT6711A",                          "LT_SPI_CS"),
    ("C_TX1P1",       "U3",        10, "ac_coupling",       "DP TX1+ AC cap",                                          "LT_TX1_P"),
    ("C_TX1N1",       "U3",        10, "ac_coupling",       "DP TX1- AC cap",                                          "LT_TX1_N"),
    ("C_TX2P1",       "U3",        10, "ac_coupling",       "DP TX2+ AC cap",                                          "LT_TX2_P"),
    ("C_TX2N1",       "U3",        10, "ac_coupling",       "DP TX2- AC cap",                                          "LT_TX2_N"),
    ("C_AUX_P1",      "U3",        10, "ac_coupling",       "AUX+ AC cap",                                             "LT_AUX_P"),
    ("C_AUX_N1",      "U3",        10, "ac_coupling",       "AUX- AC cap",                                             "LT_AUX_N"),
    ("D_TX1",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX1 ESD clamp to USB-C OUT connector",                "DP_TX1_N"),
    ("D_TX2",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX2 ESD clamp to USB-C OUT connector",                "DP_TX2_N"),
    ("D_AUX1",        "J_USB_OUT1", 8, "esd_clamp",         "DP AUX ESD clamp to USB-C OUT connector",                "DP_AUX_N"),
    ("D_USB1",        "J_USB_IN1",  8, "esd_clamp",         "USB D+/D- ESD clamp to USB-C IN connector",              "USB_N"),
    ("U5",            "U3",        25, "decoupling_bulk",   "AP7333-3.3 LDO near LT6711A",                            "LT_3V3"),
    ("U6",            "U3",        20, "decoupling_bulk",   "AP7333-1.8 LDO near LT6711A",                            "LT_1V8"),
    ("U7",            "U3",        20, "decoupling_bulk",   "NCP1117-1.2 LDO near LT6711A",                           "LT_1V2"),
    ("C_LT3V3_BULK1", "U5",         8, "decoupling_bypass", "LT_3V3 bulk cap at AP7333-3.3 output",                   "LT_3V3"),
    ("C_LT3V3_BYP1",  "U5",         8, "decoupling_bypass", "LT_3V3 bypass cap at AP7333-3.3 output",                 "LT_3V3"),
    ("C_U5_IN1",      "U5",         8, "decoupling_bypass", "U5 input bypass at AP7333-3.3 VIN",                      "+5V"),
    ("C_LT1V8_BULK1", "U6",         8, "decoupling_bypass", "LT_1V8 bulk cap at AP7333-1.8 output",                   "LT_1V8"),
    ("C_LT1V8_BYP1",  "U3",         8, "decoupling_bypass", "LT_1V8 bypass cap at LT6711A LT_1V8 input",             "LT_1V8"),
    ("C_LT1V2_BULK1", "U7",         8, "decoupling_bypass", "LT_1V2 bulk cap at NCP1117-1.2 output",                  "LT_1V2"),
    ("C_LT1V2_BYP1",  "U7",         8, "decoupling_bypass", "LT_1V2 bypass cap at NCP1117-1.2 output",               "LT_1V2"),
    ("C_U7_IN1",      "U7",         8, "decoupling_bypass", "U7 input bypass at NCP1117-1.2 VIN",                     "LT_3V3"),
    ("C_FLASH_BYP1",  "U8",         8, "decoupling_bypass", "SPI flash bypass cap at W25Q32JV VCC",                   "LT_3V3"),
    ("C_STUSB_VDD1",  "U2",         8, "decoupling_bypass", "STUSB4500 VDD bypass cap (CM5_3V3)",                     "CM5_3V3"),
    ("C_STUSB_1V2",   "U2",         8, "decoupling_bypass", "STUSB4500 1.2V internal bypass cap",                     "STUSB_1V2"),
    ("C_STUSB_2V7",   "U2",         8, "decoupling_bypass", "STUSB4500 2.7V internal bypass cap",                     "STUSB_2V7"),
    ("C_STUSB_VSYS1", "U2",         8, "decoupling_bypass", "STUSB4500 VSYS bypass cap (USBC_VBUS)",                  "USBC_VBUS"),
    ("R_STSCL1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SCL pull-up to U2",                            "STUSB_SCL"),
    ("R_STSDA1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SDA pull-up to U2",                            "STUSB_SDA"),
    ("IC1",           "J_PWR_IN1", 13, "power_mgmt",        "LTC4412 main power ideal diode near power input connector", "PWR_IN"),
    ("IC2",           "J_USB_IN1", 25, "power_mgmt",        "LTC4412 USB ideal diode near USB-C input connector",     "USBC_VBUS"),
    ("Q2",            "IC1",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC1",                 "VMAIN"),
    ("Q3",            "IC2",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC2",                 "USBC_VBUS"),
    ("D_TVS",         "J_PWR_IN1", 15, "filter",            "TVS clamp at power input connector",                     "PWR_IN"),
    ("D_TVS",         "IC1",       15, "filter",            "TVS clamp near ideal diode controller IC1",              "PWR_IN"),
    ("C_VBAT1",       "SOM1",       8, "decoupling_bypass", "VBAT RTC bypass cap near SOM1 pin 76",                   "CM5_3V3"),
    ("R_TSDA1",       "J_DSI1",    20, "pullup_pulldown",   "GT911 touch I2C SDA pull-up near J_DSI1",               "TOUCH_SDA"),
    ("R_BOOT1",       "SOM1",      20, "pullup_pulldown",   "nRPIBOOT pull-up near SOM1",                            "nRPIBOOT"),
    ("R_TRST1",       "J_DSI1",    20, "pullup_pulldown",   "touch reset pull-up near DSI connector",                 "TOUCH_RST"),
    ("R_TSCL1",       "J_DSI1",    20, "pullup_pulldown",   "touch I2C SCL pull-up near J_DSI1",                     "TOUCH_SCL"),
    ("J_DEBUG1",      "SOM1",      30, "pullup_pulldown",   "UART debug header near SOM1",                           "GPIO14"),
    ("J_PWR_BTN1",    "SOM1",      40, "pullup_pulldown",   "power button header near SOM1",                         "PWR_BTN"),
]

FACE_OVERRIDES = {
    ("C_BOOT1",       "U1"):        "left",
    ("C_SS1",         "U1"):        "right",
    ("L1",            "U1"):        "left",
    ("R_BOT1",        "U1"):        "right",
    ("C_OUT1",        "L1"):        "right",
    ("U5",            "U3"):        "left",
    ("U7",            "U3"):        "left",
    ("C_U5_IN1",      "U5"):        "left",
    ("C_LT3V3_BULK1", "U5"):        "down",
    ("C_LT1V8_BULK1", "U6"):        "right",
    ("C_LT1V2_BULK1", "U7"):        "up",
    ("C_LT1V2_BYP1",  "U7"):        "right",
    ("C_U7_IN1",      "U7"):        "up",
    ("C_FLASH_BYP1",  "U8"):        "down",
    ("IC2",           "J_USB_IN1"): "left",
    ("D_TVS",         "IC1"):       "up",
    ("R_TSDA1",       "J_DSI1"):    "up",
    ("R_TSCL1",       "J_DSI1"):    "up",
    ("J_DEBUG1",      "SOM1"):      "right",
}

# ── MATH ──────────────────────────────────────────────────────────────────────
def rot2d(x, y, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return x*c - y*s, x*s + y*c

def quantize_face(fx, fy):
    if abs(fx) >= abs(fy):
        return 'right' if fx >= 0 else 'left'
    return 'down' if fy >= 0 else 'up'

def cardinal_dir(face):
    return {'left': (-1,0), 'right': (1,0), 'up': (0,-1), 'down': (0,1)}[face]

# ── LOAD DATA ─────────────────────────────────────────────────────────────────
def load_backup_positions(path):
    pos = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 4 and parts[0] not in ('Ref', '--'):
                try:
                    pos[parts[0]] = (float(parts[1]), float(parts[2]), float(parts[3]))
                except ValueError:
                    pass
    return pos

def db_face_for_net(anchor_ref, net_hint, geom_db, fp_rotations):
    """Mirrors the fixed logic in place_by_proximity_rules.py."""
    if not net_hint or anchor_ref not in geom_db:
        return None
    entry = geom_db[anchor_ref]
    anchor_rot = fp_rotations.get(anchor_ref, 0.0)

    pads_on_net = [p for p in entry.get("pads", []) if p.get("net") == net_hint]
    if pads_on_net:
        votes = {'left': 0, 'right': 0, 'up': 0, 'down': 0}
        for pad in pads_on_net:
            lx, ly = pad["local_xy"]
            if abs(lx) < 1e-6 and abs(ly) < 1e-6:
                continue
            wx, wy = rot2d(lx, ly, anchor_rot)
            votes[quantize_face(wx, wy)] += 1
        if sum(votes.values()) > 0:
            best = max(votes, key=votes.get)
            return best, cardinal_dir(best)

    dominant = entry.get("net_dominant_face", {}).get(net_hint)
    if not dominant:
        return None
    lx, ly = cardinal_dir(dominant)
    wx, wy = rot2d(lx, ly, anchor_rot)
    best = quantize_face(wx, wy)
    return best, cardinal_dir(best)

def backup_face(sat_ref, anchor_ref, positions):
    """Face direction of satellite relative to anchor from clean backup positions."""
    if sat_ref not in positions or anchor_ref not in positions:
        return None
    ax, ay, _ = positions[anchor_ref]
    sx, sy, _ = positions[sat_ref]
    dx, dy = sx - ax, sy - ay
    if abs(dx) < 1e-3 and abs(dy) < 1e-3:
        return "coincident"
    return quantize_face(dx, dy)

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    geom_db   = json.load(open(GEOM_DB_PATH))
    positions = load_backup_positions(BACKUP_POS)
    fp_rotations = {ref: positions[ref][2] for ref in positions}

    print(f"{'Satellite':<18} {'Anchor':<14} {'Net':<16} {'Backup':>8} {'Script':>8}  Result")
    print("-" * 82)

    mismatches = 0
    no_data    = 0
    overrides  = 0

    for rule in RULES:
        sat, anchor = rule[0], rule[1]
        net = rule[5] if len(rule) > 5 else None

        ref_face = backup_face(sat, anchor, positions)

        override = FACE_OVERRIDES.get((sat, anchor))
        if override:
            script_face = override
            tag = "OVERRIDE"
            overrides += 1
        else:
            result = db_face_for_net(anchor, net, geom_db, fp_rotations)
            script_face = result[0] if result else "no-data"
            tag = ""

        if ref_face is None:
            tag = "MISSING"
            no_data += 1
        elif ref_face == "coincident":
            tag = "COINCIDENT"
        elif script_face == "no-data":
            tag = "NO-DATA"
            no_data += 1
        elif ref_face != script_face and not override:
            tag = "*** MISMATCH ***"
            mismatches += 1
        elif not tag:
            tag = "ok"

        print(f"{sat:<18} {anchor:<14} {net:<16} {str(ref_face):>8} {str(script_face):>8}  {tag}")

    print("-" * 82)
    print(f"Mismatches: {mismatches}   No-data: {no_data}   Overrides: {overrides}   OK: {len(RULES)-mismatches-no_data-overrides}")

if __name__ == "__main__":
    main()
