"""
Compute differential trace width (W) and spacing (S) for impedance-controlled
HS routing. Implements IPC-2141A closed-form formulas for differential microstrip
(outer layers) and differential stripline (buried inner layers).

Run once per HS pair class after the Phase 6 stackup is confirmed. Record the
output W/S values in PROJECT_PARAMS and use them in the .kicad_dru file.
"""

import math

# ── PROJECT CONFIG ─────────────────────────────────────────────────────────────
# Stackup values — fill in from your fabricator's confirmed stackup spec.
H_MM  = 0.21    # dielectric thickness between signal layer and adjacent GND plane (mm)
T_MM  = 0.035   # copper thickness (mm): 1 oz = 0.035 mm, 2 oz = 0.070 mm
ER    = 4.3     # dielectric constant (FR4: 4.2–4.5; use 4.3 for JLC/PCBWay)

# Target differential impedances to solve for — one entry per HS pair class.
# Keys are labels; values are target Z_diff in ohms.
TARGETS = {
    "DP / HDMI (100 ohm)": 100,
    "USB3 SS    ( 90 ohm)":  90,
}

# Routing layer for HS signals — determines which formula applies.
# "inner" = buried stripline (preferred for HS); "outer" = microstrip (escape only).
ROUTING_LAYER_TYPE = "inner"   # "inner" or "outer"
# ── END CONFIG ─────────────────────────────────────────────────────────────────


def microstrip_diff(H, T, er, Zdiff):
    """
    Differential microstrip (outer layer).
    Returns (W_mm, S_mm).
    Approximation: solve single-ended width then apply empirical differential correction.
    """
    # Single-ended microstrip width for Z0 = Zdiff / 2 (loosely coupled approximation)
    Z0 = Zdiff / 2.0
    W = (5.98 * H) / (math.exp(Z0 * math.sqrt(er) / 87.0) - 0.8 * T)
    W_diff = W * 0.85      # empirical correction for tight coupling
    S_diff = W_diff        # gap ≈ width for 100-ohm differential
    return W_diff, S_diff


def stripline_diff(H, T, er, Zdiff):
    """
    Differential stripline (buried inner layer, symmetric, IPC-2141A).
    Inverts the closed-form formula to solve for W given Zdiff.
    Returns (W_mm, S_mm).
    """
    term = math.exp(Zdiff * math.sqrt(er) / 120.0)   # note: 120 not 60 for differential
    W = (4.0 * H / (0.67 * math.pi * term)) - (T / 0.8)
    if W <= 0:
        raise ValueError(
            f"Computed W={W:.4f} mm <= 0 — Zdiff={Zdiff} ohm unreachable with "
            f"H={H} mm, T={T} mm, er={er}. Increase H or reduce Zdiff target."
        )
    S = 2.0 * W    # for 100-ohm differential on same reference planes
    return W, S


print("=" * 60)
print("IMPEDANCE CALCULATION RESULTS")
print(f"  H={H_MM} mm  T={T_MM} mm  εr={ER}  layer={ROUTING_LAYER_TYPE}")
print("=" * 60)

for label, zdiff in TARGETS.items():
    try:
        if ROUTING_LAYER_TYPE == "inner":
            W, S = stripline_diff(H_MM, T_MM, ER, zdiff)
        else:
            W, S = microstrip_diff(H_MM, T_MM, ER, zdiff)

        print(f"\n{label}")
        print(f"  Z_diff target : {zdiff} ohm")
        print(f"  Trace width W : {W:.3f} mm")
        print(f"  Gap spacing S : {S:.3f} mm")
        print(f"  .kicad_dru    : (constraint track_width (min {W:.3f}mm) (opt {W:.3f}mm) (max {W:.3f}mm))")

    except ValueError as e:
        print(f"\n{label}  ERROR: {e}")

print()
print("=" * 60)
print("Record W and S in PROJECT_PARAMS and use in the .kicad_dru HighSpeed rule class.")
print("Validate with your fabricator's impedance calculator before ordering.")
print("=" * 60)
