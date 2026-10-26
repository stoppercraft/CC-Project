"""
debug_highspeed.py
Algorithm verification harness for route_highspeed.py.

Runs the full upstream pipeline (clear → fanout → critical) then routes only
the pairs listed in TARGET_PAIRS and writes results to the board so they can
be inspected in KiCad.

Configure TARGET_PAIRS below, then run directly:
  python debug_highspeed.py

DO NOT run DRC or board inspection after this script — the board shows partial
HS routing only (TARGET_PAIRS, no other pairs). That partial state does not
represent a real routing run. Use Route and Verify for full validation.
"""
import sys, os, subprocess

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PYTHON      = sys.executable

sys.path.insert(0, SCRIPTS_DIR)
import routing_config as cfg

PCB   = cfg.PCB_FILE
BOARD = PCB

# ── Configure target pair(s) ──────────────────────────────────────────────────
# One or more pair names from routing_config.HS_PAIRS.
# Example:  TARGET_PAIRS = ['HDMI0_CLK', 'HDMI0_TX0']
TARGET_PAIRS = ['HDMI0_CLK']

# ── Step 1 — Clear board ──────────────────────────────────────────────────────
print("=== Step 1: clear board ===")
result = subprocess.run(
    [PYTHON, "-c",
     "import sys; sys.path.insert(0,'C:/Program Files/KiCad/10.0/bin/Lib/site-packages');"
     "import pcbnew;"
     f"board=pcbnew.LoadBoard(r'{BOARD}');"
     "to_remove=[t for t in board.GetTracks() if not t.IsLocked()];"
     "[board.Remove(t) for t in to_remove];"
     "board.Save(board.GetFileName());"
     "print(f'Cleared {len(to_remove)} tracks/vias')"],
    capture_output=True, text=True)
cleared_line = [l for l in result.stdout.splitlines() if l.startswith("Cleared")]
print(cleared_line[0] if cleared_line else "(cleared)")

# ── Step 2a — Fanout ─────────────────────────────────────────────────────────
print("\n=== Step 2a: route_fanout_vias.py --apply ===")
result = subprocess.run(
    [PYTHON, os.path.join(SCRIPTS_DIR, "route_fanout_vias.py"), "--apply"],
    capture_output=True, text=True)
for line in result.stdout.splitlines():
    if any(k in line for k in ("via(s) placed", "Saved →", "keepout", "pocket-push")):
        print(line)

# ── Step 2b — Critical ───────────────────────────────────────────────────────
print("\n=== Step 2b: route_critical.py --apply ===")
result = subprocess.run(
    [PYTHON, os.path.join(SCRIPTS_DIR, "route_critical.py"), "--apply"],
    capture_output=True, text=True)
for line in result.stdout.splitlines():
    if any(k in line for k in ("CONFLICT", "anchor-reloc", "Board saved", "full-net")):
        print(line)

# ── Step 3 — Route target pairs only ────────────────────────────────────────
pairs_arg = ",".join(TARGET_PAIRS)
print(f"\n=== Step 3: route_highspeed.py --apply --pairs {pairs_arg} ===")
result = subprocess.run(
    [PYTHON, os.path.join(SCRIPTS_DIR, "route_highspeed.py"),
     "--apply", "--pairs", pairs_arg],
    capture_output=True, text=True)
print(result.stdout)
if result.returncode != 0:
    print(f"[exit code {result.returncode} — conflicts present]")

print(f"\nBoard written with {TARGET_PAIRS} routed.")
print("Reload in KiCad to inspect the pair geometry.")
print("DO NOT run DRC or Route and Verify — board is partial.")
