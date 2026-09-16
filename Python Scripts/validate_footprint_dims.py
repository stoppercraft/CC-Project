"""
validate_footprint_dims.py
Validates IC footprint dimensions (pitch, pad size, EP size) against
tolerances derived from datasheets or IPC-7351 land patterns.

Does NOT require pcbnew — reads .kicad_mod files directly.

Usage: python validate_footprint_dims.py
"""

import re
import math
import os

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
# Standard KiCad footprint library path
KICAD_FP = r"C:/Program Files/KiCad/10.0/share/kicad/footprints"

# Your project's custom footprint library (.pretty folder), or "" if none
CUSTOM_FP = r"[PROJECT_DIR]\[PROJECT_NAME].pretty"

# List of ICs to validate.
# Each entry: (label, library_or_None, footprint_name, exp_pitch, pitch_tol,
#              pad_L_range, pad_W_range, ep_w_range, ep_h_range, notes)
#
# library_or_None: e.g. "Package_SO.pretty"  — use None for custom library
# exp_pitch: expected pitch in mm
# pitch_tol: acceptable pitch deviation in mm
# pad_L_range, pad_W_range: (min, max) mm for pad long/short axis, or None to skip
# ep_w_range, ep_h_range:   (min, max) mm for EP (exposed pad), or None to skip
#
# Example:
# CHECKS = [
#     ("U1 Power IC SOIC-8",
#      "Package_SO.pretty",
#      "SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm",
#      1.27, 0.05,
#      (1.4, 2.1), (0.5, 0.7),
#      None, None,
#      "SOIC-8 with exposed thermal pad"),
# ]
CHECKS = [
    # (label, lib, fp_name, exp_pitch, pitch_tol, pad_L_range, pad_W_range, ep_w_range, ep_h_range, notes)
]
# ───────────────────────────────────────────────────────────────────────────


def read_mod(lib, name):
    """Read a .kicad_mod file. Tries standard lib, then custom lib."""
    if lib:
        path = f"{KICAD_FP}/{lib}/{name}.kicad_mod"
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                return f.read(), path
    path = f"{CUSTOM_FP}/{name}.kicad_mod"
    if os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return f.read(), path
    return None, None


def get_pads(content):
    """Extract all pads: list of dicts with num, x, y, w, h."""
    pads = []
    for m in re.finditer(
        r'\(pad\s+"([^"]*)"[^)]*\(at\s+([-\d.]+)\s+([-\d.]+)(?:\s+[-\d.]+)?\)'
        r'\s*\(size\s+([\d.]+)\s+([\d.]+)\)',
        content, re.DOTALL
    ):
        pads.append({
            'num': m.group(1),
            'x': float(m.group(2)),
            'y': float(m.group(3)),
            'w': float(m.group(4)),
            'h': float(m.group(5))
        })
    return pads


def calc_pitch(pads):
    """Calculate minimum center-to-center pitch among signal pads."""
    signal = [p for p in pads if re.match(r'^\d+$', p['num'])]
    if len(signal) < 2:
        signal = [p for p in pads if p['num'] and p['num'] not in ('EP', '')]
    if len(signal) < 2:
        return None
    min_d = 999.0
    for i in range(len(signal)):
        for j in range(i + 1, len(signal)):
            d = math.sqrt((signal[i]['x'] - signal[j]['x']) ** 2 +
                          (signal[i]['y'] - signal[j]['y']) ** 2)
            if 0.25 < d < 2.5:
                min_d = min(min_d, d)
    return round(min_d, 4) if min_d < 999 else None


def get_ep(pads):
    """Get EP (exposed pad) dimensions as (width, height) or (None, None)."""
    if not pads:
        return None, None
    sorted_pads = sorted(pads, key=lambda p: p['w'] * p['h'], reverse=True)
    largest = sorted_pads[0]
    if len(sorted_pads) > 1:
        second = sorted_pads[1]
        if largest['w'] * largest['h'] > second['w'] * second['h'] * 3:
            return round(largest['w'], 4), round(largest['h'], 4)
    if largest['w'] > 1.0 and largest['h'] > 1.0:
        return round(largest['w'], 4), round(largest['h'], 4)
    return None, None


def get_signal_pad(pads):
    """Get representative signal pad dimensions (length, width)."""
    signal = [p for p in pads if re.match(r'^\d+$', p['num'])]
    if not signal:
        return None, None
    areas = [p['w'] * p['h'] for p in signal]
    med_area = sorted(areas)[len(areas) // 2]
    typical = [p for p in signal if p['w'] * p['h'] <= med_area * 3]
    if typical:
        p = typical[0]
        L = max(p['w'], p['h'])
        W = min(p['w'], p['h'])
        return round(L, 4), round(W, 4)
    return None, None


def run_check(label, lib, fp_name, exp_pitch, pitch_tol,
              pad_L_range, pad_W_range, ep_w_range, ep_h_range, notes):
    """Run dimension check for one IC and return result dict."""
    content, fpath = read_mod(lib, fp_name)
    if content is None:
        return {
            'label': label, 'fp': fp_name, 'status': 'FAIL',
            'reason': f'File not found (lib={lib}, custom={CUSTOM_FP})',
            'pitch': None, 'pad_L': None, 'pad_W': None, 'ep_w': None, 'ep_h': None
        }

    pads  = get_pads(content)
    pitch = calc_pitch(pads)
    pad_L, pad_W = get_signal_pad(pads)
    ep_w, ep_h   = get_ep(pads)
    fails = []

    if pitch is None:
        fails.append("Cannot determine pitch")
    elif abs(pitch - exp_pitch) > pitch_tol:
        fails.append(f"Pitch {pitch:.3f}mm outside {exp_pitch}±{pitch_tol}mm")

    if pad_L_range and pad_L is not None:
        lo, hi = pad_L_range
        if not (lo <= pad_L <= hi):
            fails.append(f"Pad length {pad_L:.3f}mm outside [{lo},{hi}]mm")

    if pad_W_range and pad_W is not None:
        lo, hi = pad_W_range
        if not (lo <= pad_W <= hi):
            fails.append(f"Pad width {pad_W:.3f}mm outside [{lo},{hi}]mm")

    if ep_w_range and ep_w is not None:
        lo, hi = ep_w_range
        if not (lo <= ep_w <= hi):
            fails.append(f"EP width {ep_w:.3f}mm outside [{lo},{hi}]mm")

    if ep_h_range and ep_h is not None:
        lo, hi = ep_h_range
        if not (lo <= ep_h <= hi):
            fails.append(f"EP height {ep_h:.3f}mm outside [{lo},{hi}]mm")

    status = "PASS" if not fails else "FAIL"
    reason = "; ".join(fails) if fails else "All dimensions within tolerance"
    if notes and status == "PASS":
        reason += f" [{notes}]"

    return {
        'label': label, 'fp': fp_name, 'fp_path': fpath,
        'status': status, 'reason': reason,
        'pitch': pitch, 'pad_L': pad_L, 'pad_W': pad_W,
        'ep_w': ep_w, 'ep_h': ep_h
    }


# ── Main ─────────────────────────────────────────────────────────────────────
results = []
for row in CHECKS:
    (label, lib, fp_name, exp_pitch, pitch_tol,
     pad_L_range, pad_W_range, ep_w_range, ep_h_range, notes) = row
    result = run_check(label, lib, fp_name, exp_pitch, pitch_tol,
                       pad_L_range, pad_W_range, ep_w_range, ep_h_range, notes)
    results.append(result)

print("=" * 80)
print("FOOTPRINT DIMENSION VALIDATION")
print("=" * 80)
print()

all_pass = True
for r in results:
    if r['status'] == 'FAIL':
        all_pass = False
    print(f"[{r['status']}] {r['label']}")
    print(f"       Footprint: {r['fp']}")
    print(f"       pitch={r['pitch']}mm, pad={r['pad_L']}x{r['pad_W']}mm, EP={r['ep_w']}x{r['ep_h']}mm")
    print(f"       Result: {r['reason']}")
    print()

print("=" * 80)
print(f"OVERALL: {'PASS — all checks within tolerance' if all_pass else 'FAIL — see items above'}")
print("=" * 80)
