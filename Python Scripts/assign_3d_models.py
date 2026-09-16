"""
assign_3d_models.py
Assigns 3D models to footprints that don't already have one.
Models are matched by footprint name pattern against the KiCad standard 3D library.
Uses ${KICAD8_3DMODEL_DIR} path variable in model paths so they are portable.

Usage: python assign_3d_models.py
"""

import sys
import os
import shutil
import time

# ── PROJECT CONFIG — fill in for your project ──────────────────────────────
PCB_FILE        = r"[PROJECT_DIR]\[PROJECT_NAME].kicad_pcb"
REPORTS_DIR     = r"[PROJECT_DIR]\Reports"
BACKUPS_DIR     = r"[PROJECT_DIR]\Backups"
KICAD_SITE_PKGS = r"C:/Program Files/KiCad/10.0/bin/Lib/site-packages"
# Physical path to the KiCad 3D model library (for scanning what exists)
MODEL_BASE      = r"C:/Program Files/KiCad/10.0/share/kicad/3dmodels"
# KiCad path variable used in .kicad_pcb model references (portable)
MODEL_VAR       = "${KICAD8_3DMODEL_DIR}"
# ───────────────────────────────────────────────────────────────────────────

sys.path.insert(0, KICAD_SITE_PKGS)
import pcbnew

REPORT_PATH = os.path.join(REPORTS_DIR, "3d_models_report.txt")

# ── BACKUP ─────────────────────────────────────────────────────────────────
os.makedirs(BACKUPS_DIR, exist_ok=True)
tag = time.strftime("%Y%m%d_%H%M%S")
shutil.copy(PCB_FILE, os.path.join(BACKUPS_DIR, f"assign_3d_models.{tag}.bak"))
print(f"Backup saved to {BACKUPS_DIR}")

# ── STEP 1: Inventory available 3D models ──────────────────────────────────
print("=== Scanning 3D model library ===")
available_models = {}  # base_name_lower -> (subdir, filename)
for subdir in os.listdir(MODEL_BASE):
    subdir_path = os.path.join(MODEL_BASE, subdir)
    if os.path.isdir(subdir_path):
        for fname in os.listdir(subdir_path):
            if fname.lower().endswith('.step') or fname.lower().endswith('.wrl'):
                base = os.path.splitext(fname)[0].lower()
                available_models[base] = (subdir, fname)

print(f"Found {len(available_models)} models in {MODEL_BASE}")


def find_model(subdir, stem):
    """Find model by subdir and stem (without extension). Returns full ${VAR} path or None."""
    for ext in ['.step', '.wrl']:
        if os.path.isfile(os.path.join(MODEL_BASE, subdir, stem + ext)):
            return f"{MODEL_VAR}/{subdir}/{stem}{ext}"
    return None


# ── STEP 2: Define mapping rules ───────────────────────────────────────────
# Extend this function to add your project-specific IC packages.
# fp_name = fp.GetFPID().GetLibItemName()  e.g. "R_0402_1005Metric"
# fp_lib  = fp.GetFPID().GetLibNickname()  e.g. "Resistor_SMD"
# ref     = fp.GetReference()              e.g. "R1"
def determine_model(fp):
    """Return the model path string for a given footprint, or None if no match."""
    import re as _re
    fp_id   = fp.GetFPID()
    fp_name = str(fp_id.GetLibItemName())
    fp_lib  = str(fp_id.GetLibNickname())
    ref     = fp.GetReference()

    m_prefix = _re.match(r'^([A-Za-z]+)', ref)
    ref_prefix = m_prefix.group(1).upper() if m_prefix else ''.join(c for c in ref if c.isalpha()).upper()

    fp_name_l = fp_name.lower()
    fp_lib_l  = fp_lib.lower()

    # ── Resistors ────────────────────────────────────────────────────────
    if ref_prefix in ('R', 'RN') and '0402' in fp_name_l:
        if 'array' in fp_name_l or ref_prefix == 'RN':
            path = find_model("Resistor_SMD.3dshapes", "R_Array_Concave_4x0402")
            if path:
                return path
        return find_model("Resistor_SMD.3dshapes", "R_0402_1005Metric")

    if ref_prefix == 'R' and '0805' in fp_name_l:
        return find_model("Resistor_SMD.3dshapes", "R_0805_2012Metric")

    if ref_prefix == 'R' and '1206' in fp_name_l:
        return find_model("Resistor_SMD.3dshapes", "R_1206_3216Metric")

    if 'r_array' in fp_name_l or ('array' in fp_name_l and ref_prefix in ('R', 'RN')):
        if '4x0402' in fp_name_l:
            path = find_model("Resistor_SMD.3dshapes", "R_Array_Concave_4x0402")
            if path:
                return path
        return find_model("Resistor_SMD.3dshapes", "R_Array_Concave_4x0603")

    # ── Capacitors ───────────────────────────────────────────────────────
    if ref_prefix in ('C', 'CN') and '0402' in fp_name_l:
        return find_model("Capacitor_SMD.3dshapes", "C_0402_1005Metric")

    if ref_prefix == 'C' and '0805' in fp_name_l:
        return find_model("Capacitor_SMD.3dshapes", "C_0805_2012Metric")

    if ref_prefix == 'C' and '1206' in fp_name_l:
        return find_model("Capacitor_SMD.3dshapes", "C_1206_3216Metric")

    if 'cp_elec' in fp_name_l or 'elec_' in fp_name_l:
        if '6.3' in fp_name_l:
            path = find_model("Capacitor_SMD.3dshapes", "CP_Elec_6.3x5.4")
            if path:
                return path
        if '5x5' in fp_name_l or '5.4' in fp_name_l:
            path = find_model("Capacitor_SMD.3dshapes", "CP_Elec_5x5.4")
            if path:
                return path
        return find_model("Capacitor_SMD.3dshapes", "CP_Elec_6.3x5.4")

    # ── Inductors ────────────────────────────────────────────────────────
    if ref_prefix == 'L':
        if 'srr1260' in fp_name_l or 'bourns_srr1260' in fp_name_l:
            path = find_model("Inductor_SMD.3dshapes", "L_Bourns_SRR1260")
            if path:
                return path
        if 'sdr0604' in fp_name_l or 'bourns_sdr0604' in fp_name_l:
            path = find_model("Inductor_SMD.3dshapes", "L_Bourns_SDR0604")
            if path:
                return path
        return find_model("Inductor_SMD.3dshapes", "L_0402_1005Metric")

    # ── Fuses ────────────────────────────────────────────────────────────
    if ref_prefix == 'F' or 'fuse' in fp_name_l:
        if '1206' in fp_name_l:
            path = find_model("Fuse.3dshapes", "Fuse_1206_3216Metric_Pad1.42x1.75mm_HandSoldering")
            if path:
                return path
            return find_model("Fuse.3dshapes", "Fuse_1206_3216Metric")

    # ── Diodes ───────────────────────────────────────────────────────────
    if ref_prefix == 'D':
        if 'sod-123' in fp_name_l:
            return find_model("Diode_SMD.3dshapes", "D_SOD-123")
        if 'd_sma' in fp_name_l or 'sma' in fp_name_l:
            return find_model("Diode_SMD.3dshapes", "D_SMA")
        if 'd_smb' in fp_name_l or 'smb' in fp_name_l:
            return find_model("Diode_SMD.3dshapes", "D_SMB")
        if 'sot-363' in fp_name_l or 'sc-70-6' in fp_name_l:
            return find_model("Package_TO_SOT_SMD.3dshapes", "SOT-363_SC-70-6")

    if 'sot-363' in fp_name_l or 'sc-70-6' in fp_name_l:
        return find_model("Package_TO_SOT_SMD.3dshapes", "SOT-363_SC-70-6")

    # ── Transistors / MOSFETs ─────────────────────────────────────────────
    if ref_prefix == 'Q':
        if 'sot-23' in fp_name_l:
            return find_model("Package_TO_SOT_SMD.3dshapes", "SOT-23")

    # ── IC packages ───────────────────────────────────────────────────────
    if 'lqfp-32' in fp_name_l:
        return find_model("Package_QFP.3dshapes", "LQFP-32_7x7mm_P0.8mm")

    if 'soic-8' in fp_name_l or 'soic_8' in fp_name_l:
        return find_model("Package_SO.3dshapes", "SOIC-8_3.9x4.9mm_P1.27mm")

    if 'sot-23-5' in fp_name_l:
        return find_model("Package_TO_SOT_SMD.3dshapes", "SOT-23-5")

    if 'qfn-56' in fp_name_l or ('qfn' in fp_name_l and '56' in fp_name_l):
        path = find_model("Package_DFN_QFN.3dshapes", "QFN-56-1EP_8x8mm_P0.5mm_EP4.5x5.2mm")
        if path:
            return path
        return find_model("Package_DFN_QFN.3dshapes", "QFN-56-1EP_7x7mm_P0.4mm_EP5.6x5.6mm")

    if 'qfn-48' in fp_name_l or ('qfn' in fp_name_l and '48' in fp_name_l):
        path = find_model("Package_DFN_QFN.3dshapes", "QFN-48-1EP_7x7mm_P0.5mm_EP5.15x5.15mm")
        if path:
            return path
        return find_model("Package_DFN_QFN.3dshapes", "VQFN-48-1EP_7x7mm_P0.5mm_EP4.1x4.1mm")

    if 'wqfn-16' in fp_name_l or ('qfn-16' in fp_name_l and '3x3' in fp_name_l):
        path = find_model("Package_DFN_QFN.3dshapes", "WQFN-16-1EP_3x3mm_P0.5mm_EP1.6x1.6mm")
        if path:
            return path
        return find_model("Package_DFN_QFN.3dshapes", "QFN-16-1EP_3x3mm_P0.5mm_EP1.7x1.7mm")

    if 'qfn-20' in fp_name_l or ('qfn' in fp_name_l and '20' in fp_name_l):
        path = find_model("Package_DFN_QFN.3dshapes", "QFN-20-1EP_4x5mm_P0.5mm_EP2.65x3.65mm")
        if path:
            return path
        return find_model("Package_DFN_QFN.3dshapes", "QFN-20-1EP_4x4mm_P0.5mm_EP2.5x2.5mm")

    if 'tssop-10' in fp_name_l or 'essop-10' in fp_name_l or ('tssop' in fp_name_l and '10' in fp_name_l):
        path = find_model("Package_SO.3dshapes", "TSSOP-10_3x3mm_P0.5mm")
        if path:
            return path
        return find_model("Package_SO.3dshapes", "SSOP-10_3.9x4.9mm_P1mm")

    # ── Connectors ───────────────────────────────────────────────────────
    if 'hdmi' in fp_name_l or 'hdmi' in fp_lib_l:
        path = find_model("Connector_Video.3dshapes", "HDMI_A_Amphenol_10029449-x01xLF_Horizontal")
        if path:
            return path
        return find_model("Connector_Video.3dshapes", "HDMI_A_Contact_Technology_19APL2_Horizontal")

    if 'usb_c' in fp_name_l or 'usbc' in fp_name_l or 'usb-c' in fp_name_l:
        path = find_model("Connector_USB.3dshapes", "USB_C_Receptacle_GCT_USB4085")
        if path:
            return path
        return find_model("Connector_USB.3dshapes", "USB_C_Receptacle_Amphenol_12401610E4-2A")

    # ── Pin headers ───────────────────────────────────────────────────────
    if 'pinheader' in fp_name_l and 'p2.54' in fp_name_l:
        import re
        m = re.search(r'_1x(\d+)_', fp_name)
        if m:
            n = int(m.group(1))
            path = find_model("Connector_PinHeader_2.54mm.3dshapes",
                              f"PinHeader_1x{n:02d}_P2.54mm_Vertical")
            if path:
                return path

    # ── Tag-Connect ───────────────────────────────────────────────────────
    if 'tag-connect' in fp_name_l or 'tc2030' in fp_name_l:
        return None  # No standard 3D model available

    # ── Test points ───────────────────────────────────────────────────────
    if ref_prefix == 'TP' or 'testpoint' in fp_name_l or 'testpoint' in fp_lib_l:
        return find_model("TestPoint.3dshapes", "TestPoint_Loop_D2.50mm_Drill1.0mm")

    # ── Mounting holes ────────────────────────────────────────────────────
    if ref_prefix in ('MH', 'H') or 'mountinghole' in fp_name_l:
        return None

    return None


# ── STEP 3: Load PCB and assign models ─────────────────────────────────────
print(f"\n=== Loading PCB: {PCB_FILE} ===")
board = pcbnew.LoadBoard(PCB_FILE)

assigned = []
skipped_has_model = []
unmatched = []

for fp in board.GetFootprints():
    models = fp.Models()
    if len(models) > 0:
        skipped_has_model.append(fp.GetReference())
        continue

    model_path = determine_model(fp)
    if model_path:
        model = pcbnew.FP_3DMODEL()
        model.m_Filename = model_path
        model.m_Offset   = pcbnew.VECTOR3D(0, 0, 0)
        model.m_Scale    = pcbnew.VECTOR3D(1, 1, 1)
        model.m_Rotation = pcbnew.VECTOR3D(0, 0, 0)
        models.push_back(model)
        assigned.append((fp.GetReference(), model_path))
        print(f"  ASSIGNED {fp.GetReference()} -> {model_path}")
    else:
        fp_name = str(fp.GetFPID().GetLibItemName())
        unmatched.append((fp.GetReference(), fp_name))
        print(f"  UNMATCHED {fp.GetReference()} ({fp_name})")

# ── STEP 4: Save PCB ────────────────────────────────────────────────────────
print("\n=== Saving PCB ===")
board.Save(PCB_FILE)
mtime = os.path.getmtime(PCB_FILE)
print(f"  Saved at: {time.ctime(mtime)}")

# ── STEP 5: Write report ────────────────────────────────────────────────────
os.makedirs(REPORTS_DIR, exist_ok=True)
report_lines = []
report_lines.append("=" * 70)
report_lines.append("3D MODEL ASSIGNMENT REPORT")
report_lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}")
report_lines.append("=" * 70)
report_lines.append("")
report_lines.append(f"Total footprints: {len(assigned) + len(skipped_has_model) + len(unmatched)}")
report_lines.append(f"Already had model (skipped): {len(skipped_has_model)}")
report_lines.append(f"Newly assigned: {len(assigned)}")
report_lines.append(f"Could not match: {len(unmatched)}")
report_lines.append("")
report_lines.append("--- ASSIGNED ---")
for ref, path in sorted(assigned):
    report_lines.append(f"  {ref:20s}  {path}")
report_lines.append("")
report_lines.append("--- ALREADY HAD MODEL (skipped) ---")
for ref in sorted(skipped_has_model):
    report_lines.append(f"  {ref}")
report_lines.append("")
report_lines.append("--- UNMATCHED (no model found) ---")
for ref, fp_name in sorted(unmatched):
    report_lines.append(f"  {ref:20s}  ({fp_name})")
report_lines.append("")
report_lines.append("=" * 70)
report_lines.append("END OF REPORT")

report_text = "\n".join(report_lines)
with open(REPORT_PATH, 'w') as f:
    f.write(report_text)

print("\n" + report_text)
print(f"\nReport written to: {REPORT_PATH}")
