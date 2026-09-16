"""
find_close_net_refs.py — List which pads/footprints connect to a set of nets

For each net of interest, prints the footprint references and pad numbers that
carry that net. Useful for verifying differential-pair connectivity and finding
which components are endpoints for any net.

Usage:
    python find_close_net_refs.py

Fill in the PROJECT CONFIG block before running.
"""

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"C:\path\to\project\ProjectName.kicad_pcb"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"

# Dictionary of group_name -> (net_A, net_B) pairs to inspect.
# Each net will be listed with all pads connected to it.
PAIRS = {
    "PAIR_0": ("/NET0P", "/NET0N"),
    "PAIR_1": ("/NET1P", "/NET1N"),
    # add more as needed
}
# ─────────────────────────────────────────────────────────────────────────────

import sys
sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

board = pcbnew.LoadBoard(PCB_FILE)

for group, (net_a, net_b) in PAIRS.items():
    print(f"\n{group}")
    for net_name in (net_a, net_b):
        entries = {}
        for fp in board.GetFootprints():
            for pad in fp.Pads():
                if pad.GetNetname() == net_name:
                    entries.setdefault(fp.GetReference(), []).append(pad.GetNumber())
        parts = ",  ".join(
            f"{ref} pad {'/'.join(sorted(pads))}"
            for ref, pads in sorted(entries.items())
        )
        print(f"  {net_name}: {parts if parts else '(no pads found)'}")
