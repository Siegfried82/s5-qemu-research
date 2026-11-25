#!/usr/bin/env python3
"""Extract original startup payloads and identify the task delaying AVIO startup.

Reads a saved flat RAM snapshot only. Does not set mailbox flags, patch firmware,
change task priority, or claim that a coprocessor instruction has executed.
"""
from pathlib import Path
import argparse, hashlib, json, mmap, struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASE = 0x06280000
ap = argparse.ArgumentParser()
ap.add_argument('--snapshot', type=Path, default=ROOT/'s5-emulator/menu-smp/results/pulse-timer-60s')
ap.add_argument('--output', type=Path, default=HERE/'coprocessor-wait-evidence.json')
args = ap.parse_args()
source = ROOT/'s5-v29-offline/unpacked-v29/03_program.bin'
program = source.read_bytes()
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
boot = json.loads((args.snapshot/'result.json').read_text())
with (args.snapshot/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    def task(t, stack=False):
        out = {
            'tcb': hex(t), 'name': ram[t+0xc4:t+0xd4].split(b'\0')[0].decode('ascii','replace'),
            'id': u32(t+0xc0), 'entry': hex(u32(t+0xd8)), 'run_count': u32(t+4),
            'state': u32(t+0x30), 'itron_state': hex(u32(t+0xec)),
            'priority': u32(t+0x2c), 'wait_type': u32(t+0x100),
            'wait_object_id': u32(t+0x104), 'remaining_ticks': hex(u32(t+0x4c)),
            'saved_sp': hex(u32(t+8)), 'cpu_assignment': u32(t+0x94),
            'cpu_flags_raw': hex(u32(t+0x98)),
        }
        if stack:
            out['stack_words'] = {hex(a):hex(u32(a)) for a in range(u32(t+8),u32(t+16)+1,4)}
        return out
    current = [u32(0x20040+4*i) for i in range(3)]
    runnable = []
    pos = 0x08d00000
    while (pos := ram.find(b'DRHT',pos,0x09000000)) >= 0:
        if ram[pos+0xc4:pos+0xc9] == b'uiTSK' and u32(pos+0x30) == 0:
            runnable.append(task(pos))
        pos += 4
    result = {
        'scope': 'Original firmware bytes and saved RAM; no mailbox or task mutation',
        'source': str(source), 'source_sha256': sha,
        'snapshot': str((args.snapshot/'ram.bin').resolve()),
        'final_registers': boot['final_registers'],
        'current_tasks': [hex(t) for t in current],
        'main_task': task(0x08dfe890,True),
        'avio_task18': task(0x08dffe14,True),
        'busy_task66': task(0x08e03ad4,True),
        'runnable_tasks_sorted_by_priority': sorted(runnable,key=lambda x:x['priority']),
        'diagnosis': {
            'irq_at_stop': 'CPU0 PC0x18/CPSR IRQ is a sampling point at a periodic interrupt; GIC active0/pending36 does not show an IRQ handler stuck',
            'interrupted_pc': '0x065491f0 from LR_irq0x065491f4 and saved task context',
            'busy_loop': '0x065491ee LDR r0,[r4];0x065491f0 CMP r0,#0;0x065491f2 BEQ0x065491ee; r4=0x7b004000',
            'task18_status': 'Activated and runnable, priority108, run_count0; task66 priority56 is executing a non-yielding wait on CPU0',
            'bounded_run_limit': 'This specific busy loop has no timeout or task yield; unlike the earlier I2C retry, more wall time alone supplies no missing peer state',
            'call_chain': ['0x0675efae ->0x0676ede0','saved0x0676ee29','saved0x065c593b','saved0x065850fb','0x0657bb98 ->0x065491e4'],
            'stale_saved_lr': '0x0655197b is also present in saved context, but is not treated as an immediate call-chain frame; helpers can leave a stale LR after POP PC',
        },
        'original_startup': {
            'host_entry': '0x065491e4', 'upload_setup': '0x065493c0',
            'upload_api': '0x064e6b34 ->0x064b12f6',
            'copy_loop': '0x064b13ea LDRword from source then STRword to target; not DMA',
            'control': '0x7c14071c (instance0); setup ORs bits2/3, then bit1; final0x064b140c clears bit3',
            'boot_address_register': '0x7c140778 (instance0:0x7c14071c+0x5c)',
            'initial_boot_address_value': '0x1000 (setup parameter0x2000 shifted right1)',
            'code': {'source':'0x06a2d918','target':'0x7b020000','bytes':65536},
            'data': {'source':'0x06a3d918','target':'0x7b000000','bytes':131072},
            'architecture': 'Not identified. Original embedded instruction-like stream starts010100a72a0000a7; do not label it an ARM core or a specific DSP ISA without evidence.',
        },
        'mailbox_protocol': {
            'base':'0x7b004000',
            'phase1': 'Host waits u32[base+0] !=0',
            'shared_pointers': 'Host takes u32[base+0xc] and u32[base+0x10] as halfword offsets, translates to0x7b000000+(offset<<1)',
            'checks': [
                {'pointer':'first','offset':'0x68','width':16,'expected':'0x1234'},
                {'pointer':'second','offset':'0x160','width':32,'expected':'0x1234'},
                {'pointer':'second','offset':'0x3208','width':32,'expected':'0x5678'},
                {'pointer':'second','offset':'0x40e8','width':32,'expected':'0x9012'},
                {'pointer':'second','offset':'0x4370','width':32,'expected':'0x3456'},
                {'pointer':'second','offset':'0x4524','width':32,'expected':'0x7890'},
                {'pointer':'second','offset':'0x79f0','width':32,'expected':'0x5678'},
            ],
            'phase2': 'Host calls0x0652e8c0(first pointer), writesu32[base+4]=1, waits u32[base+8]!=0, then stores further shared-structure pointers',
            'provenance_limit': 'These are consumer requirements, not permission to manufacture valid structures or signal peer success.',
        },
        'menu_rendered': False,
    }
    assert current[0] == 0x08e03ad4
    assert result['avio_task18']['run_count'] == 0 and result['avio_task18']['state'] == 0
    assert result['busy_task66']['priority'] < result['avio_task18']['priority']

payloads=[]
for name,address,size in [('embedded-peer-code.bin',0x06a2d918,65536),('embedded-peer-data.bin',0x06a3d918,131072)]:
    blob=program[address-BASE:address-BASE+size]
    (HERE/name).write_bytes(blob)
    payloads.append({'file':str(HERE/name),'original_address':hex(address),'bytes':size,
                     'sha256':hashlib.sha256(blob).hexdigest(),'nonzero_bytes':sum(x!=0 for x in blob)})
result['extracted_original_payloads']=payloads
args.output.write_text(json.dumps(result,indent=2)+'\n')

lines=[f'Original03_program.bin sha256={sha}; Thumb disassembly, literal pools may decode as instructions.']
for a,n in [(0x065491e4,0x100),(0x065493c0,0x68),(0x064b12f6,0x12e),(0x0675ef84,0x76),(0x0657bb98,8),(0x065850f4,0xe)]:
    lines.append('\nRANGE '+hex(a))
    md=capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
    for i in md.disasm(program[a-BASE:a-BASE+n],a):
        lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
args.output.with_suffix('.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Wrote task, mailbox and original payload evidence; snapshot assertions passed.')
