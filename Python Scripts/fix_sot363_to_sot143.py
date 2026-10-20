"""
One-shot script: replaces all 5 SOT-363_SC-70-6 footprint blocks in the PCB
with correct Package_TO_SOT_SMD:SOT-143 footprints.

Preserves: footprint UUID, position, rotation, Reference text/uuid/position,
           Value text/uuid/position.
Replaces:  all graphical elements, pads (6-pin -> 4-pin), model, lib_id.
Net assignments (SOT-143 pinout: pad1=GND, pad2=I/O1, pad3=I/O2, pad4=NC):
  D_CC1:  GND / USBC_IN_CC1 / USBC_IN_CC2
  D_USB1: GND / USB_N       / USB_P
  D_AUX1: GND / DP_AUX_P   / DP_AUX_N
  D_TX1:  GND / DP_TX1_N   / DP_TX1_P
  D_TX2:  GND / DP_TX2_N   / DP_TX2_P
"""

import re
import uuid

PCB_PATH = r'E:\Claude Projects\Frame Line Device\Frameline Generator PCB Version 2\Frameline_Compute_V2.kicad_pcb'

NETS = {
    'D_CC1':  ('GND', 'USBC_IN_CC1', 'USBC_IN_CC2'),
    'D_USB1': ('GND', 'USB_N',        'USB_P'),
    'D_AUX1': ('GND', 'DP_AUX_P',    'DP_AUX_N'),
    'D_TX1':  ('GND', 'DP_TX1_N',    'DP_TX1_P'),
    'D_TX2':  ('GND', 'DP_TX2_N',    'DP_TX2_P'),
}


def extract_fp_block(content, start_pos):
    depth = 0
    i = start_pos
    while i < len(content):
        if content[i] == '(':
            depth += 1
        elif content[i] == ')':
            depth -= 1
            if depth == 0:
                return content[start_pos:i+1], i+1
        i += 1
    return None, None


def make_sot143_block(fp_uuid, x, y, rot,
                      ref_text, ref_uuid, ref_at_x, ref_at_y, ref_at_rot, ref_hide,
                      val_uuid, val_at_x, val_at_y, val_at_rot, val_hide,
                      ds_uuid, desc_uuid,
                      net_pad1, net_pad2, net_pad3):
    """Build a complete SOT-143 footprint block string."""
    pad1_net = f'\n\t\t\t(net "{net_pad1}")' if net_pad1 else ''
    pad2_net = f'\n\t\t\t(net "{net_pad2}")' if net_pad2 else ''
    pad3_net = f'\n\t\t\t(net "{net_pad3}")' if net_pad3 else ''
    pad1_uuid = str(uuid.uuid4())
    pad2_uuid = str(uuid.uuid4())
    pad3_uuid = str(uuid.uuid4())
    pad4_uuid = str(uuid.uuid4())
    u_fab = str(uuid.uuid4())

    rot_str = f' {rot}' if rot != '0' else ''
    ref_hide_str = '\n\t\t\t(hide yes)' if ref_hide else ''
    val_hide_str = '\n\t\t\t(hide yes)' if val_hide else ''

    lines = [
        f'\t(footprint "Package_TO_SOT_SMD:SOT-143"',
        f'\t\t(layer "F.Cu")',
        f'\t\t(uuid "{fp_uuid}")',
        f'\t\t(at {x} {y}{rot_str})',
        f'\t\t(descr "SOT-143 https://www.nxp.com/docs/en/package-information/SOT143B.pdf")',
        f'\t\t(tags "SOT-143")',
        f'\t\t(property "Reference" "{ref_text}"',
        f'\t\t\t(at {ref_at_x} {ref_at_y} {ref_at_rot})',
        f'\t\t\t(layer "F.SilkS"){ref_hide_str}',
        f'\t\t\t(uuid "{ref_uuid}")',
        f'\t\t\t(effects',
        f'\t\t\t\t(font',
        f'\t\t\t\t\t(size 1 1)',
        f'\t\t\t\t\t(thickness 0.15)',
        f'\t\t\t\t)',
        f'\t\t\t)',
        f'\t\t)',
        f'\t\t(property "Value" "PRTR5V0U2X"',
        f'\t\t\t(at {val_at_x} {val_at_y} {val_at_rot})',
        f'\t\t\t(layer "F.Fab"){val_hide_str}',
        f'\t\t\t(uuid "{val_uuid}")',
        f'\t\t\t(effects',
        f'\t\t\t\t(font',
        f'\t\t\t\t\t(size 1 1)',
        f'\t\t\t\t\t(thickness 0.15)',
        f'\t\t\t\t)',
        f'\t\t\t)',
        f'\t\t)',
        f'\t\t(property "Datasheet" ""',
        f'\t\t\t(at 0 0 {rot})',
        f'\t\t\t(unlocked yes)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(hide yes)',
        f'\t\t\t(uuid "{ds_uuid}")',
        f'\t\t\t(effects',
        f'\t\t\t\t(font',
        f'\t\t\t\t\t(size 1.27 1.27)',
        f'\t\t\t\t\t(thickness 0.15)',
        f'\t\t\t\t)',
        f'\t\t\t)',
        f'\t\t)',
        f'\t\t(property "Description" ""',
        f'\t\t\t(at 0 0 {rot})',
        f'\t\t\t(unlocked yes)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(hide yes)',
        f'\t\t\t(uuid "{desc_uuid}")',
        f'\t\t\t(effects',
        f'\t\t\t\t(font',
        f'\t\t\t\t\t(size 1.27 1.27)',
        f'\t\t\t\t\t(thickness 0.15)',
        f'\t\t\t\t)',
        f'\t\t\t)',
        f'\t\t)',
        f'\t\t(attr smd)',
        f'\t\t(duplicate_pad_numbers_are_jumpers no)',
        # SilkS lines
        f'\t\t(fp_line',
        f'\t\t\t(start -0.65 1.56)',
        f'\t\t\t(end 0.65 1.56)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.12)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.SilkS")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start 0.65 -1.56)',
        f'\t\t\t(end -0.62 -1.56)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.12)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.SilkS")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        # Pin-1 triangle
        f'\t\t(fp_poly',
        f'\t\t\t(pts',
        f'\t\t\t\t(xy -1 -1.51) (xy -1.24 -1.84) (xy -0.76 -1.84) (xy -1 -1.51)',
        f'\t\t\t)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.12)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(fill yes)',
        f'\t\t\t(layer "F.SilkS")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        # CrtYd
        f'\t\t(fp_line',
        f'\t\t\t(start -1.6 1.7)',
        f'\t\t\t(end -1.6 -1.7)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.05)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.CrtYd")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start -1.6 1.7)',
        f'\t\t\t(end 1.6 1.7)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.05)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.CrtYd")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start 1.6 -1.7)',
        f'\t\t\t(end -1.6 -1.7)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.05)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.CrtYd")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start 1.6 -1.7)',
        f'\t\t\t(end 1.6 1.7)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.05)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.CrtYd")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        # F.Fab outline with chamfer
        f'\t\t(fp_line',
        f'\t\t\t(start -0.65 -0.95)',
        f'\t\t\t(end -0.15 -1.45)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.1)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start -0.65 1.45)',
        f'\t\t\t(end -0.65 -0.95)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.1)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start -0.15 -1.45)',
        f'\t\t\t(end 0.65 -1.45)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.1)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start 0.65 -1.45)',
        f'\t\t\t(end 0.65 1.45)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.1)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        f'\t\t(fp_line',
        f'\t\t\t(start 0.65 1.45)',
        f'\t\t\t(end -0.65 1.45)',
        f'\t\t\t(stroke',
        f'\t\t\t\t(width 0.1)',
        f'\t\t\t\t(type solid)',
        f'\t\t\t)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{str(uuid.uuid4())}")',
        f'\t\t)',
        # Fab reference text
        f'\t\t(fp_text user "${{REFERENCE}}"',
        f'\t\t\t(at 0 0 90)',
        f'\t\t\t(layer "F.Fab")',
        f'\t\t\t(uuid "{u_fab}")',
        f'\t\t\t(effects',
        f'\t\t\t\t(font',
        f'\t\t\t\t\t(size 0.5 0.5)',
        f'\t\t\t\t\t(thickness 0.075)',
        f'\t\t\t\t)',
        f'\t\t\t)',
        f'\t\t)',
        # Pad 1 — GND, large pad
        f'\t\t(pad "1" smd roundrect',
        f'\t\t\t(at -1 -0.75)',
        f'\t\t\t(size 0.7 1)',
        f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")',
        f'\t\t\t(roundrect_rratio 0.25){pad1_net}',
        f'\t\t\t(uuid "{pad1_uuid}")',
        f'\t\t)',
        # Pad 2 — I/O1
        f'\t\t(pad "2" smd roundrect',
        f'\t\t\t(at -1 0.95)',
        f'\t\t\t(size 0.7 0.6)',
        f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")',
        f'\t\t\t(roundrect_rratio 0.25){pad2_net}',
        f'\t\t\t(uuid "{pad2_uuid}")',
        f'\t\t)',
        # Pad 3 — I/O2
        f'\t\t(pad "3" smd roundrect',
        f'\t\t\t(at 1 0.95)',
        f'\t\t\t(size 0.7 0.6)',
        f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")',
        f'\t\t\t(roundrect_rratio 0.25){pad3_net}',
        f'\t\t\t(uuid "{pad3_uuid}")',
        f'\t\t)',
        # Pad 4 — VCC/NC
        f'\t\t(pad "4" smd roundrect',
        f'\t\t\t(at 1 -0.95)',
        f'\t\t\t(size 0.7 0.6)',
        f'\t\t\t(layers "F.Cu" "F.Mask" "F.Paste")',
        f'\t\t\t(roundrect_rratio 0.25)',
        f'\t\t\t(uuid "{pad4_uuid}")',
        f'\t\t)',
        # End
        f'\t\t(embedded_fonts no)',
        f'\t\t(model "${{KICAD10_3DMODEL_DIR}}/Package_TO_SOT_SMD.3dshapes/SOT-143.step"',
        f'\t\t\t(offset',
        f'\t\t\t\t(xyz 0 0 0)',
        f'\t\t\t)',
        f'\t\t\t(scale',
        f'\t\t\t\t(xyz 1 1 1)',
        f'\t\t\t)',
        f'\t\t\t(rotate',
        f'\t\t\t\t(xyz 0 0 0)',
        f'\t\t\t)',
        f'\t\t)',
        f'\t)',
    ]
    return '\n'.join(lines)


def main():
    with open(PCB_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    starts = [m.start() for m in re.finditer(r'\(footprint "SOT-363_SC-70-6"', content)]
    print(f'Found {len(starts)} SOT-363 footprints to replace')

    replacements = []  # (start, end, new_block, ref_text)

    for start in starts:
        block, end = extract_fp_block(content, start)

        # Extract footprint UUID (first uuid in block header)
        fp_uuid = re.search(r'\(uuid "([^"]+)"\)', block[:200]).group(1)

        # Extract at position
        at_match = re.search(r'^\t\t\(at ([\-\d\.]+) ([\-\d\.]+)(?: ([\-\d\.]+))?\)', block, re.MULTILINE)
        x = at_match.group(1)
        y = at_match.group(2)
        rot = at_match.group(3) if at_match.group(3) else '0'

        # Extract Reference property
        ref_match = re.search(
            r'\(property "Reference" "([^"]+)"\s*'
            r'\(at ([\-\d\.]+) ([\-\d\.]+) ([\-\d\.]+)\)\s*'
            r'\(layer "[^"]+"\)\s*'
            r'((?:\(hide yes\)\s*)?)'
            r'\(uuid "([^"]+)"\)',
            block
        )
        ref_text = ref_match.group(1)
        ref_at_x = ref_match.group(2)
        ref_at_y = ref_match.group(3)
        ref_at_rot = ref_match.group(4)
        ref_hide = 'hide' in ref_match.group(5)
        ref_uuid = ref_match.group(6)

        # Extract Value property
        val_match = re.search(
            r'\(property "Value" "[^"]*"\s*'
            r'\(at ([\-\d\.]+) ([\-\d\.]+) ([\-\d\.]+)\)\s*'
            r'\(layer "[^"]+"\)\s*'
            r'((?:\(hide yes\)\s*)?)'
            r'\(uuid "([^"]+)"\)',
            block
        )
        val_at_x = val_match.group(1)
        val_at_y = val_match.group(2)
        val_at_rot = val_match.group(3)
        val_hide = 'hide' in val_match.group(4)
        val_uuid = val_match.group(5)

        ds_uuid = str(uuid.uuid4())
        desc_uuid = str(uuid.uuid4())

        nets = NETS[ref_text]
        new_block = make_sot143_block(
            fp_uuid, x, y, rot,
            ref_text, ref_uuid, ref_at_x, ref_at_y, ref_at_rot, ref_hide,
            val_uuid, val_at_x, val_at_y, val_at_rot, val_hide,
            ds_uuid, desc_uuid,
            nets[0], nets[1], nets[2]
        )

        replacements.append((start, end, new_block, ref_text))
        print(f'  Prepared {ref_text}: at({x},{y},{rot}) old={len(block)} new={len(new_block)}')

    # Apply in reverse order to preserve character offsets
    new_content = content
    for start, end, new_block, ref_text in sorted(replacements, key=lambda r: r[0], reverse=True):
        new_content = new_content[:start] + new_block + new_content[end:]
        print(f'  Applied {ref_text}')

    with open(PCB_PATH, 'w', encoding='utf-8') as f:
        f.write(new_content)

    print('PCB file written.')

    # Verify
    with open(PCB_PATH, 'r', encoding='utf-8') as f:
        verify = f.read()
    sot363_count = verify.count('SOT-363_SC-70-6')
    sot143_count = len(re.findall(r'Package_TO_SOT_SMD:SOT-143"', verify))
    print(f'Verify: SOT-363 remaining={sot363_count}, SOT-143 in file={sot143_count}')

    if sot363_count == 0 and sot143_count == 5:
        print('SUCCESS: all 5 footprints replaced.')
    else:
        print('WARNING: counts do not match expected values.')


if __name__ == '__main__':
    main()
