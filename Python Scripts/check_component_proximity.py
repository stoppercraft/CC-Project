"""
check_component_proximity.py  —  Phase 8 check
Schematic-aware proximity verification using PROXIMITY_RULES_TABLE from PROJECT_PARAMS.

Measurement priority per rule:
  1. net_hint specified and found on both refs → measure on that net only   [net:NETNAME]
  2. net_hint specified but not found on one ref → fallback to min shared net [hint-miss:NETNAME]
  3. net_hint is None → minimum across all shared nets                      [pad:NETNAME]
  4. No shared net exists → centroid-to-centroid fallback                   [ctr]

Writes Reports/component_proximity_report.txt.
"""

import sys, math, os, pathlib

KICAD_BIN = "C:/Program Files/KiCad/10.0/bin"
os.environ["PATH"] = KICAD_BIN + os.pathsep + os.environ.get("PATH", "")
sys.path.insert(0, KICAD_BIN + "/Lib/site-packages")
import pcbnew

# PROJECT CONFIG — set from PROJECT_PARAMS before running
PCB_FILE    = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb"
REPORT_FILE = r"E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Reports\component_proximity_report.txt"

# RULES — from PROXIMITY_RULES_TABLE in PROJECT_PARAMS (net_hint=None → min shared net)
RULES = [
    # TPS54561 buck converter (U1)
    ("C_VIN1",        "U1",        15, "decoupling_bulk",   "input bulk cap to TPS54561 VIN",                         "VMAIN"),
    ("C_BOOT1",       "U1",         8, "bootstrap",         "bootstrap cap to TPS54561 BOOT/SW",                      "BUCK_SW"),
    ("C_SS1",         "U1",         8, "decoupling_bypass", "soft-start cap to TPS54561 SS",                          "BUCK_SS"),
    ("C_OUT1",        "U1",        15, "decoupling_bulk",   "output bulk cap to TPS54561 SW node",                    None),
    ("L1",            "U1",        12, "filter",            "buck inductor to TPS54561 SW",                           "BUCK_SW"),
    ("R_TOP1",        "U1",        20, "pullup_pulldown",   "feedback top resistor to TPS54561 VSENSE",               "VSENSE_5V"),
    ("R_BOT1",        "U1",        20, "pullup_pulldown",   "feedback bottom resistor to TPS54561 VSENSE",            "VSENSE_5V"),
    ("R_COMP1",       "U1",        12, "filter",            "compensation resistor to TPS54561 COMP",                 "BUCK_COMP"),
    ("C_COMP1",       "U1",        12, "filter",            "compensation cap to TPS54561 COMP",                      None),
    # LT6711A HDMI→DP converter (U3)
    ("Y1",            "U3",        12, "crystal",           "27MHz crystal to LT6711A",                               "LT_XTAL_IN"),
    ("C_XTAL_IN1",    "U3",        12, "crystal",           "crystal load cap (IN) to LT6711A",                       "LT_XTAL_IN"),
    ("C_XTAL_OUT1",   "U3",        12, "crystal",           "crystal load cap (OUT) to LT6711A",                      "LT_XTAL_OUT"),
    ("R_REXT1",       "U3",         8, "decoupling_bypass", "REXT bandgap resistor to LT6711A REXT pin",              "LT_REXT"),
    ("R_RST1",        "U3",        20, "pullup_pulldown",   "LT_RST_N pull-up to LT6711A",                           "LT_RST_N"),
    ("R_HPD1",        "U3",        20, "pullup_pulldown",   "HDMI HPD pull-up to LT6711A",                           "HDMI0_HPD"),
    ("R_HDMI_SDA1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SDA pull-up to LT6711A",                       "HDMI0_SDA"),
    ("R_HDMI_SCL1",   "U3",        20, "pullup_pulldown",   "HDMI I2C SCL pull-up to LT6711A",                       "HDMI0_SCL"),
    ("R_VDET_H1",     "U3",        12, "filter",            "VBUS detect divider (top) to LT6711A",                  "VBUS_DET"),
    ("R_VDET_L1",     "U3",        12, "filter",            "VBUS detect divider (bot) to LT6711A",                  "VBUS_DET"),
    ("U8",            "U3",        15, "decoupling_bulk",   "SPI flash proximity to LT6711A",                        None),
    ("C_TX1P1",       "U3",        10, "ac_coupling",       "DP TX1+ AC cap — source: LT6711A TX1P output",          "LT_TX1_P"),
    ("C_TX1N1",       "U3",        10, "ac_coupling",       "DP TX1- AC cap — source: LT6711A TX1N output",          "LT_TX1_N"),
    ("C_TX2P1",       "U3",        10, "ac_coupling",       "DP TX2+ AC cap — source: LT6711A TX2P output",          "LT_TX2_P"),
    ("C_TX2N1",       "U3",        10, "ac_coupling",       "DP TX2- AC cap — source: LT6711A TX2N output",          "LT_TX2_N"),
    ("C_AUX_P1",      "U3",        10, "ac_coupling",       "AUX+ AC cap — source: LT6711A AUXP pin",                "LT_AUX_P"),
    ("C_AUX_N1",      "U3",        10, "ac_coupling",       "AUX- AC cap — source: LT6711A AUXN pin",                "LT_AUX_N"),
    # ESD clamps
    ("D_TX1",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX1 ESD clamp to USB-C OUT connector",               "DP_TX1_N"),
    ("D_TX2",         "J_USB_OUT1", 8, "esd_clamp",         "DP TX2 ESD clamp to USB-C OUT connector",               "DP_TX2_N"),
    ("D_AUX1",        "J_USB_OUT1", 8, "esd_clamp",         "DP AUX ESD clamp to USB-C OUT connector",               "DP_AUX_N"),
    ("D_USB1",        "J_USB_IN1",  8, "esd_clamp",         "USB D+/D- ESD clamp to USB-C IN connector",             "USB_N"),
    # LDO proximity to LT6711A (no single critical net — centroid fallback acceptable)
    ("U5",            "U3",        25, "decoupling_bulk",   "AP7333-3.3 LDO near LT6711A (LDO-to-load)",             None),
    ("U6",            "U3",        20, "decoupling_bulk",   "AP7333-1.8 LDO near LT6711A",                           None),
    ("U7",            "U3",        20, "decoupling_bulk",   "NCP1117-1.2 LDO near LT6711A",                          None),
    # LDO output caps (U5 AP7333-3.3)
    ("C_LT3V3_BULK1", "U5",         8, "decoupling_bypass", "LT_3V3 bulk cap at AP7333-3.3 output",                  "LT_3V3"),
    ("C_LT3V3_BYP1",  "U5",         8, "decoupling_bypass", "LT_3V3 bypass cap at AP7333-3.3 output",                "LT_3V3"),
    ("C_U5_IN1",      "U5",         8, "decoupling_bypass", "U5 input bypass at AP7333-3.3 VIN",                     "+5V"),
    # LDO output caps (U6 AP7333-1.8)
    ("C_LT1V8_BULK1", "U6",         8, "decoupling_bypass", "LT_1V8 bulk cap at AP7333-1.8 output",                  "LT_1V8"),
    ("C_LT1V8_BYP1",  "U6",         8, "decoupling_bypass", "LT_1V8 bypass cap at AP7333-1.8 output",                "LT_1V8"),
    # LDO output caps (U7 NCP1117-1.2)
    ("C_LT1V2_BULK1", "U7",         8, "decoupling_bypass", "LT_1V2 bulk cap at NCP1117-1.2 output",                 "LT_1V2"),
    ("C_LT1V2_BYP1",  "U7",         8, "decoupling_bypass", "LT_1V2 bypass cap at NCP1117-1.2 output",               "LT_1V2"),
    ("C_U7_IN1",      "U7",         8, "decoupling_bypass", "U7 input bypass at NCP1117-1.2 VIN",                    "LT_3V3"),
    # SPI flash (U8)
    ("C_FLASH_BYP1",  "U8",         8, "decoupling_bypass", "SPI flash bypass cap at W25Q32JV VCC",                  "LT_3V3"),
    # STUSB4500 USB-PD controller (U2)
    ("C_STUSB_VDD1",  "U2",         8, "decoupling_bypass", "STUSB4500 VDD bypass cap (CM5_3V3)",                    "CM5_3V3"),
    ("C_STUSB_1V2",   "U2",         8, "decoupling_bypass", "STUSB4500 1.2V internal bypass cap",                    "STUSB_1V2"),
    ("C_STUSB_2V7",   "U2",         8, "decoupling_bypass", "STUSB4500 2.7V internal bypass cap",                    "STUSB_2V7"),
    ("C_STUSB_VSYS1", "U2",         8, "decoupling_bypass", "STUSB4500 VSYS bypass cap (USBC_VBUS)",                 "USBC_VBUS"),
    ("R_STSCL1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SCL pull-up to U2",                           "STUSB_SCL"),
    ("R_STSDA1",      "U2",        20, "pullup_pulldown",   "STUSB I2C SDA pull-up to U2",                           "STUSB_SDA"),
    # Ideal diode OR (IC1/IC2 + FETs)
    ("Q2",            "IC1",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC1",                "VMAIN"),
    ("Q3",            "IC2",        8, "decoupling_bypass", "PMV50EPEAR FET adjacent to LTC4412 IC2",                "USBC_VBUS"),
    # Power input TVS
    ("D_TVS",         "J_PWR_IN1", 15, "filter",            "TVS clamp at power input connector",                    "PWR_IN"),
    ("D_TVS",         "IC1",       15, "filter",            "TVS clamp near ideal diode controller IC1",             None),
    # Misc
    ("C_VBAT1",       "SOM1",       8, "decoupling_bypass", "VBAT RTC bypass cap near SOM1 pin 76",                  "CM5_3V3"),
    ("R_TSDA1",       "J_DSI1",    20, "pullup_pulldown",   "GT911 touch I2C SDA pull-up near J_DSI1",              "TOUCH_SDA"),
    # SOM1 peripherals
    ("R_BOOT1",       "SOM1",      20, "pullup_pulldown",   "nRPIBOOT pull-up near SOM1",                           "nRPIBOOT"),
    ("R_TRST1",       "J_DSI1",    20, "pullup_pulldown",   "touch reset pull-up near DSI connector",                "TOUCH_RST"),
    ("R_TSCL1",       "J_DSI1",    20, "pullup_pulldown",   "touch I2C SCL pull-up near J_DSI1",                    "TOUCH_SCL"),
    ("J_DEBUG1",      "SOM1",      30, "pullup_pulldown",   "UART debug header near SOM1",                          "GPIO14"),
    ("J_PWR_BTN1",    "SOM1",      40, "pullup_pulldown",   "power button header near SOM1",                        "PWR_BTN"),
]

board = pcbnew.LoadBoard(PCB_FILE)

# Build centroid map (fallback when no shared net)
centroids = {}
for fp in board.GetFootprints():
    pos = fp.GetPosition()
    centroids[fp.GetReference()] = (pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y))

# Build pad map: ref -> list of (netname, x_mm, y_mm)
pad_map = {}
for fp in board.GetFootprints():
    ref = fp.GetReference()
    pads = []
    for pad in fp.Pads():
        net = pad.GetNetname()
        pos = pad.GetPosition()
        pads.append((net, pcbnew.ToMM(pos.x), pcbnew.ToMM(pos.y)))
    pad_map[ref] = pads


def measure_dist(ref_a, ref_b, net_hint=None):
    """Return (dist_mm, method, net_used).

    method values:
      'net_hint'  — measured on the specified net (best case)
      'hint-miss' — hint net not found on one ref; fell back to min shared net
      'pad'       — no hint; minimum across all shared nets
      'ctr'       — no shared net; centroid-to-centroid fallback
    """
    pads_a = pad_map.get(ref_a, [])
    pads_b = pad_map.get(ref_b, [])

    nets_a = {}
    for net, x, y in pads_a:
        if net:
            nets_a.setdefault(net, []).append((x, y))

    # Priority 1: measure on the hint net specifically
    if net_hint:
        if net_hint in nets_a:
            min_dist = None
            for net, bx, by in pads_b:
                if net == net_hint:
                    for ax, ay in nets_a[net_hint]:
                        d = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
                        if min_dist is None or d < min_dist:
                            min_dist = d
            if min_dist is not None:
                return min_dist, "net_hint", net_hint
        # Hint net not found on one or both refs — fall through with hint-miss flag
        fallback_method = "hint-miss"
    else:
        fallback_method = "pad"

    # Priority 2: minimum across all shared nets
    min_dist = None
    best_net = None
    for net, bx, by in pads_b:
        if net and net in nets_a:
            for ax, ay in nets_a[net]:
                d = math.sqrt((ax - bx) ** 2 + (ay - by) ** 2)
                if min_dist is None or d < min_dist:
                    min_dist = d
                    best_net = net

    if min_dist is not None:
        return min_dist, fallback_method, best_net

    # Priority 3: centroid fallback
    ax, ay = centroids[ref_a]
    bx, by = centroids[ref_b]
    return math.sqrt((ax - bx) ** 2 + (ay - by) ** 2), "ctr", None


lines = [
    "COMPONENT PROXIMITY REPORT (Phase 8)",
    "Rules from PROXIMITY_RULES_TABLE in PROJECT_PARAMS",
    "Method: [net:X]=hint net used; [hint-miss:X]=hint not found, shared-net fallback; [pad:X]=min shared net; [ctr]=centroid fallback",
    "=" * 110, "",
    f"  {'Status':<6}  {'ref_A':<16}  {'ref_B':<14}  {'dist':>7}  {'max':>5}  {'type':<18}  reason",
    "  " + "-" * 106,
]

all_pass = True
n_pass = 0
n_fail = 0
n_miss = 0
fails = []

for entry in RULES:
    ref_a, ref_b, max_dist, rtype, reason, net_hint = (*entry, None)[:6] if len(entry) == 5 else entry

    if ref_a not in centroids:
        lines.append(f"  MISS    {ref_a:<16}  {'(not in PCB)':<14}  {'':>7}  {max_dist:>4}mm  {rtype:<18}  {reason}")
        n_miss += 1
        continue
    if ref_b not in centroids:
        lines.append(f"  MISS    {ref_a:<16}  {(ref_b+' ?'):<14}  {'':>7}  {max_dist:>4}mm  {rtype:<18}  {reason}")
        n_miss += 1
        continue

    dist, method, net_used = measure_dist(ref_a, ref_b, net_hint)

    if method == "net_hint":
        tag = f"[net:{net_used}]"
    elif method == "hint-miss":
        tag = f"[hint-miss:{net_hint}→{net_used or 'ctr'}]"
    elif method == "pad":
        tag = f"[pad:{net_used}]" if net_used else "[pad]"
    else:
        tag = "[ctr]"

    status = "PASS" if dist <= max_dist else "FAIL"
    if status == "FAIL":
        all_pass = False
        n_fail += 1
        fails.append((ref_a, ref_b, dist, max_dist, reason, method, net_hint, net_used))
    else:
        n_pass += 1

    lines.append(
        f"  {status:<6}  {ref_a:<16}  {ref_b:<14}  {dist:>6.1f}mm  {max_dist:>4}mm  {rtype:<18}  {reason}  {tag}"
    )

lines += ["", "=" * 110,
          f"  PASS: {n_pass}   FAIL: {n_fail}   MISSING: {n_miss}"]

if fails:
    lines += ["", "  FAIL summary (sorted by excess distance):"]
    for ref_a, ref_b, dist, max_dist, reason, method, net_hint, net_used in sorted(fails, key=lambda x: x[2]-x[3], reverse=True):
        if method == "net_hint":
            tag = f"[net:{net_used}]"
        elif method == "hint-miss":
            tag = f"[hint-miss:{net_hint}→{net_used or 'ctr'}]"
        elif method == "pad":
            tag = f"[pad:{net_used}]" if net_used else "[pad]"
        else:
            tag = "[centroid fallback]"
        lines.append(f"    {ref_a} -> {ref_b}: {dist:.1f}mm (max {max_dist}mm, over by {dist-max_dist:.1f}mm) — {reason}  {tag}")

out = "\n".join(lines)
pathlib.Path(REPORT_FILE).parent.mkdir(parents=True, exist_ok=True)
pathlib.Path(REPORT_FILE).write_text(out + "\n", encoding="utf-8")
print(out)
print(f"\n{'ALL PASS' if all_pass else 'VIOLATIONS FOUND'} — {REPORT_FILE}")
if not all_pass:
    sys.exit(1)
