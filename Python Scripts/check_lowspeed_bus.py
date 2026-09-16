"""
check_lowspeed_bus.py — Low-speed bus trace quality checker (I2C / SPI / UART etc.)

For each net in BUS_NETS:
  - Reports total routed length
  - If length exceeds LENGTH_WARN_MM, checks for GND guard traces within GUARD_DIST_MM
  - Lists all pads/footprints connected to the net

Writes a report to REPORT_FILE and prints to stdout.

Usage:
    python check_lowspeed_bus.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
REPORT_FILE     = r"C:\path\to\project\Reports\lowspeed_bus_check.txt"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Nets to check (list all low-speed bus signal nets)
BUS_NETS = [
    "/BUS_SDA",
    "/BUS_SCL",
    # add more nets as needed: "/SPI_CLK", "/SPI_MOSI", etc.
]

# Warn if any net's routed length exceeds this (mm)
LENGTH_WARN_MM = 10.0

# When length exceeds LENGTH_WARN_MM, check for GND traces within this distance (mm)
GUARD_DIST_MM = 0.3

# GND net name
GND_NET_NAME = "GND"
# ─────────────────────────────────────────────────────────────────────────────

import sys
import math
import os
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

lines = ["Low-Speed Bus Trace Quality Report", "=" * 50, ""]

for net_name in BUS_NETS:
    net = board.FindNet(net_name)
    if not net:
        lines.append(f"{net_name}: NOT FOUND in board nets")
        lines.append("")
        continue

    tracks = [t for t in board.GetTracks()
              if t.GetClass() in ("PCB_TRACK", "PCB_ARC") and t.GetNetname() == net_name]

    total_len_mm = pcbnew.ToMM(sum(t.GetLength() for t in tracks))

    lines.append(f"Net: {net_name}")
    lines.append(f"  Segments   : {len(tracks)}")
    lines.append(f"  Total length: {total_len_mm:.3f} mm")

    if total_len_mm > LENGTH_WARN_MM:
        lines.append(f"  WARNING: Length > {LENGTH_WARN_MM}mm — checking for GND guard traces")
        gnd_tracks = [t for t in board.GetTracks()
                      if t.GetClass() == "PCB_TRACK" and t.GetNetname() == GND_NET_NAME]
        missing_guard = []
        for seg in tracks:
            sx = (seg.GetStart().x + seg.GetEnd().x) / 2
            sy = (seg.GetStart().y + seg.GetEnd().y) / 2
            seg_layer = seg.GetLayer()
            guard_found = False
            for gt in gnd_tracks:
                if gt.GetLayer() != seg_layer:
                    continue
                gx = (gt.GetStart().x + gt.GetEnd().x) / 2
                gy = (gt.GetStart().y + gt.GetEnd().y) / 2
                dist_mm = math.hypot(pcbnew.ToMM(sx - gx), pcbnew.ToMM(sy - gy))
                if dist_mm <= GUARD_DIST_MM:
                    guard_found = True
                    break
            if not guard_found:
                missing_guard.append(seg)

        if missing_guard:
            lines.append(
                f"  FAIL: {len(missing_guard)}/{len(tracks)} segments lack GND guard "
                f"within {GUARD_DIST_MM}mm"
            )
        else:
            lines.append(
                f"  PASS: GND guard traces present within {GUARD_DIST_MM}mm for all segments"
            )
    else:
        lines.append(
            f"  PASS: Length {total_len_mm:.3f}mm <= {LENGTH_WARN_MM}mm — guard check not required"
        )

    # Connected pads
    net_code = net.GetNetCode()
    pads = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            if pad.GetNetCode() == net_code:
                pads.append(f"{fp.GetReference()}.{pad.GetNumber()}")
    lines.append(f"  Connected pads: {', '.join(pads) if pads else '(none)'}")
    lines.append("")

lines.append("=" * 50)
lines.append("END OF LOW-SPEED BUS CHECK")

report = "\n".join(lines)
print(report)

os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
with open(REPORT_FILE, "w", encoding="utf-8") as f:
    f.write(report)
print(f"\nReport written to: {REPORT_FILE}")
