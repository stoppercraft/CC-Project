"""
Verify thermal adequacy for every IC in the PROJECT_PARAMS thermal budget.

For each IC dissipating > 0.3 W:
  - Computes required θ_ja = (T_j_max - T_amb) / P_diss
  - Estimates achieved θ_via = 70 / (n_vias × drill_mm)  [empirical: 70°C·mm/W per via]
  - Reports PASS if achieved θ_via <= required θ_ja, FAIL otherwise
  - Suggests minimum via count to meet the thermal budget

Populate THERMAL_TABLE from the THERMAL BUDGET section of PROJECT_PARAMS.
Run as part of Loop E after Phase 9 thermal via placement.
"""

# ── PROJECT CONFIG ─────────────────────────────────────────────────────────────
REPORT_FILE  = r"[PROJECT_DIR]\Reports\thermal_report.txt"

# Ambient temperature assumption (°C) — use worst-case operating environment.
T_AMB_C = 40.0

# One entry per IC dissipating > 0.3 W.
# Fields: (ref, description, P_diss_W, T_j_max_C, n_thermal_vias, drill_mm)
# n_thermal_vias: count of vias in the exposed-pad thermal array
# drill_mm: drill diameter (typically 0.3 mm for thermal vias)
THERMAL_TABLE = [
    # Example — replace with your project's ICs:
    # ("U1", "Buck converter",    0.85, 150, 9,  0.3),
    # ("U2", "LED boost driver",  1.20, 125, 16, 0.3),
]
# ── END CONFIG ─────────────────────────────────────────────────────────────────

import os
os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

THETA_VIA_CONSTANT = 70.0   # °C·mm/W — empirical constant per thermal via

lines = []
lines.append("=" * 72)
lines.append("THERMAL ADEQUACY REPORT")
lines.append(f"  T_ambient = {T_AMB_C} °C")
lines.append("=" * 72)

all_pass = True

for ref, desc, p_diss, tj_max, n_vias, drill_mm in THERMAL_TABLE:
    required_theta = (tj_max - T_AMB_C) / p_diss
    achieved_theta = THETA_VIA_CONSTANT / (n_vias * drill_mm) if n_vias > 0 else float("inf")
    margin         = required_theta - achieved_theta
    status         = "PASS" if achieved_theta <= required_theta else "FAIL"

    if status == "FAIL":
        all_pass = False
        min_vias = int(THETA_VIA_CONSTANT / (required_theta * drill_mm)) + 1
        fix_note = f"  → need >= {min_vias} vias at {drill_mm} mm drill to meet budget"
    else:
        fix_note = ""

    lines.append(f"\n{ref}  {desc}")
    lines.append(f"  P_diss       : {p_diss:.2f} W")
    lines.append(f"  T_j_max      : {tj_max} °C")
    lines.append(f"  Required θ_ja: {required_theta:.1f} °C/W  (T_j_max − T_amb) / P_diss")
    lines.append(f"  Thermal vias : {n_vias} × {drill_mm} mm drill")
    lines.append(f"  Achieved θ_via: {achieved_theta:.1f} °C/W")
    lines.append(f"  Margin       : {margin:+.1f} °C/W  [{status}]")
    if fix_note:
        lines.append(fix_note)

lines.append("\n" + "=" * 72)
lines.append("SUMMARY: " + ("ALL PASS" if all_pass else "FAILURES PRESENT — increase via count or drill size"))
lines.append("=" * 72)

output = "\n".join(lines)
print(output)

with open(REPORT_FILE, "w") as f:
    f.write(output + "\n")

print(f"\nReport saved: {REPORT_FILE}")
