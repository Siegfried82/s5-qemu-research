#!/usr/bin/env python3
"""Rebuild LCD IRQ evidence from original bytes and a saved local experiment.

Read-only with respect to the firmware, snapshot, and guest. No IRQs, callbacks,
state flags, or menu transitions are injected by this analysis.
"""
import argparse
import hashlib
import json
import mmap
import re
import struct
from pathlib import Path

import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot', type=Path,
                    default=HERE.parent/'menu-smp/results/isolated-display-gradient')
args = parser.parse_args()
original = ROOT/'s5-v29-offline/unpacked-v29'
program = (original/'03_program.bin').read_bytes()
loader = (original/'02_loader3.bin').read_bytes()
boot = json.loads((args.snapshot/'result.json').read_text())
base = 0x06280000
word = lambda address: struct.unpack_from('<I', program, address-base)[0]

# Each tuple names the actual MOVS r0,#IRQ and the preceding LDR's literal.
registrations = [
    (196, 0x063b5956, 0x063b5ce8, 0x0649728f),
    (224, 0x063b5b4e, 0x063b5d58, 0x064972db),
    (226, 0x063b5b72, 0x063b5d60, 0x064972f3),
    (230, 0x063b5bba, 0x063b5d70, 0x06497311),
    (231, 0x063b5bcc, 0x063b5d74, 0x0649732b),
    (232, 0x063b5bde, 0x063b5d78, 0x06497339),
    (233, 0x063b5bf0, 0x063b5d7c, 0x06497353),
]
gic = {item['id']:item for item in boot['gic']['enabled_or_asserted_irqs']}
irq_evidence = []
for irq, instruction, literal, handler in registrations:
    assert struct.unpack_from('<H', program, instruction-base)[0] == 0x2000 | irq
    assert word(literal) == handler
    table_offset = 0x6978 + (irq-32)*4
    table_id, targets, config = struct.unpack_from('<HBB', loader, table_offset)
    assert table_id == irq
    irq_evidence.append({
        'gic_id':irq, 'qemu_spi_index':irq-32,
        'registration_argument_instruction':hex(instruction),
        'callback_literal':hex(literal), 'callback_thumb':hex(handler),
        'registration_api':'0x065dd09c (absolute IRQ ID, not logical device index)',
        'loader_table_offset':hex(table_offset), 'loader_targets':targets,
        'loader_configuration':config,
        'loader_configuration_meaning':'edge-triggered' if config == 2 else 'not interpreted',
        'saved_gic_state':gic.get(irq),
    })

with (args.snapshot/'ram.bin').open('rb') as stream:
    ram = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    def words(address, size):
        return {hex(offset):hex(u32(address+offset)) for offset in range(0,size,4)}
    observed = {
        'avio_completion':{'address':'0x08a9c7f8','value':u32(0x08a9c7f8),'required':2},
        'lcd_transition':{'base':'0x08ab4860','words':words(0x08ab4860,0x34),
                          'state_address':'0x08ab488c','state':u32(0x08ab488c),
                          'callback':hex(u32(0x08ab4890))},
        'primary_irq_context':{'base':'0x08ab4d8c','words':words(0x08ab4d8c,16),
                               'phase_byte':ram[0x08ab4d8d]},
        'completion_selector':{'address':'0x08ab1bba','value':ram[0x08ab1bba],
                               'active_irq_when_zero':232,'active_irq_when_nonzero':230},
        'bank0_shadow':{'base':'0x090db3d4','words':words(0x090db3d4,0x15c)},
        'bank1_shadow':{'base':'0x090db530','words':words(0x090db530,0x15c)},
        'pipeline0_descriptor':{'base':'0x090db808','mmio_base':hex(u32(0x090db828))},
        'separate_gui_wait':{'step_address':'0x08cb0ee0','step':hex(u32(0x08cb0ee0)),
                             'ack_address':'0x08aa1104','ack_byte':hex(ram[0x08aa1104]),
                             'evidence':'gui-start-wait-evidence.json'},
        'graphics_draws':boot.get('graphics_draws'), 'graphics_swaps':boot.get('graphics_swaps'),
        'snapshot_scope':'Explicit local scheduling experiment; not complete camera boot',
    }
    ram.close()

# Preserve observed ignored MMIO access lines rather than treating RAM as MMIO.
trace = args.snapshot/'qemu/qemu-trace.log'
trace_records = []
for number, line in enumerate(trace.read_text().splitlines(),1):
    match = re.search(r'addr=0x([0-9a-f]+)', line)
    if match:
        address = int(match.group(1),16)
        if (0x7c0000e0 <= address <= 0x7c0001c8
                or address in (0x7c0150dc,0x7c0150e0,0x7c050ef0,0x7c050ef4)
                or 0x7c051c00 <= address <= 0x7c051d38):
            trace_records.append({'line':number,'text':line})

initialization = []
for address in range(0x06ff2b50,0x06ff2d10,8):
    register, value = word(address), word(address+4)
    if 0x7c0000e0 <= register <= 0x7c0001c8:
        initialization.append({'table_address':hex(address),'mmio_address':hex(register),'value':hex(value)})

report = {
    'scope':'Static original instruction chains plus saved execution evidence; no new hardware model or event injection',
    'snapshot':str(args.snapshot.resolve()),
    'original_files':{
        'program':{'path':str(original/'03_program.bin'),'base':hex(base),'sha256':hashlib.sha256(program).hexdigest()},
        'loader3':{'path':str(original/'02_loader3.bin'),'base':'0x0','sha256':hashlib.sha256(loader).hexdigest()},
    },
    'observed':observed,
    'irqs':irq_evidence,
    'primary_chain':[
        'GIC196 -> 0649728e -> 0648fdd8 -> 06481598',
        '06481598 calls 06480b9e(3,0) -> 06489ae0(bank0,phase0)',
        '06489ae0 refuses bank+0x150==4; phase0 additionally requires [7c00011c]&1 == 1',
        'On eligible phase0, 0648995e commits dirty shadow words to bank MMIO then 064898ac advances per-subphase state',
        'Return 0xa8c permits 06481598 to change LCD state1 to2 and select completion source via byte08ab1bba',
    ],
    'completion_chain':[
        'GIC230 -> 06497310 only when byte08ab1bba!=0; GIC232 -> 06497338 only when byte==0',
        'Selected handler drains sibling pending IRQ231 or233 via 06497a54, then calls 0649694e',
        '0649694e polls pending GIC224 in a bounded loop; if found, clears GIC pending and calls 0648b7c4',
        'Then 0648b728 -> 0648fe4e -> 064815c8 advances state2->3, next call state3->4',
        'State3 invokes callback(8); state4 clears callback pointer and invokes callback(9)',
        'Original callback0647dcf6 maps8->completion1 and9->completion2; 0647dd0c waits for2 using original dly',
    ],
    'mmio_contract':{
        'phase0_status':{'address':'0x7c00011c','bit':0,
                         'proven_consumer':'06489b18..06489b20 requires1; 0648fdd8 also tracks this bit',
                         'uncertainty':'Physical field polarity, reset semantics, and period not identified'},
        'phase1_status':{'address':'0x7c050ef0','bit':28,
                         'proven_consumer':'065f0274 gets pipeline0 descriptor+0x20 then reads+0x2f0 bit28',
                         'uncertainty':'Physical status name and transition timing not identified; not a writable success bit'},
        'bank0':{'shadow':'0x090db3d4','stride':'0x15c','mmio_base_field':'0x144','mmio_base':'0x7c051c00',
                 'dirty_all':'0x134','dirty_parameters':'0x138','dirty_minor':'0x13c',
                 'subphase_states':['0x148','0x14c'],'bank_state':'0x150',
                 'commit':'0648995e copies original shadow offsets to corresponding MMIO offsets and clears shadow dirty flags',
                 'latch_sequences':'0648984e can write MMIO+0x98 or+0x10c as0 then1; meaning remains unproven'},
        'clock_gating':{'registers':['0x7c0150dc','0x7c0150e0'],
                         'original_functions':['0x06492ed0','0x0649321c','0x06493448','0x06493680'],
                         'evidence':'Read-modify-write clock/configuration paths, original writes ignored in saved QEMU trace',
                         'uncertainty':'No single global enable mask or source frequency established'},
        'interrupt_enable':{'proven':'Original loader routes/configures these GIC IRQs; saved GIC state has them enabled for CPU0',
                            'unknown':'Device-side interrupt masks and their register names are not yet identified'},
        'acknowledgement':{'proven':'06497a54/0649694e use 0649fa90 to read GIC pending, 0649fa4c to clear GICD_ICPENDR(+0x280)',
                           'gic_alias':'0x70001000 + CPU*0x8000',
                           'unknown':'This does not prove any display-device W1C or acknowledgement register'},
    },
    'original_initialization_table':initialization,
    'period_uncertainty':{
        'observed_register':'0x7c000134','observed_value':'0x6df91','decimal':450449,
        'source_table':'0x06ff2c38','trace':'Original table value was written twice and ignored',
        'arithmetic_only':'If the clock were27MHz and this were a reload-minus-one value,27000000/(450449+1)=59.94005994005994Hz',
        'not_verified':['27MHz source for this block','reload-minus-one semantics','frame rate meaning','timer start/stop/enable bits'],
        'implementation_status':'No timing model implemented from this arithmetic',
    },
    'trace':{'path':str(trace.resolve()),'selected_lines':trace_records},
    'conclusion':'In this snapshot the original LCD state remains1 with its callback pending and shadow config dirty. Its registered GIC sources are enabled but have no saved pending/active/level indication; the relevant MMIO is unmodeled. This is consistent with missing display timing events. A single end snapshot does not prove an IRQ never occurred.',
    'model_boundary':'A future explicit display-timing model must derive status transitions and IRQ timing from supported register programming, retain unsupported behavior as unknown, and let original handlers commit configuration and invoke callbacks. Do not force callback9, completion2, GUI ack bits, or menu state.',
    'independent_blocker':'Even completing LCD synchronization would not consume the real task66 queue76 startup request. The unknown coprocessor execution remains independently blocking GUI mode transition; see gui-start-wait-evidence.json.',
}
(HERE/'display-clock-evidence.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

cs = capstone.Cs(capstone.CS_ARCH_ARM,capstone.CS_MODE_THUMB)
cs.detail = True
ranges = [
    (0x063b594e,0x063b5964),(0x063b5b46,0x063b5b84),(0x063b5bb2,0x063b5bfa),
    (0x065dd09c,0x065dd0fa),
    (0x0647dcf6,0x0647dd30),(0x06481292,0x064813a4),
    (0x06481598,0x0648163e),(0x0648fdd8,0x0648fe4e),(0x0648fe4e,0x0648fe9e),
    (0x06480b9e,0x06480bcc),(0x06489ae0,0x06489b04),(0x06489b18,0x06489b4e),
    (0x0648984e,0x064898ac),(0x06489930,0x06489ae0),(0x065f0274,0x065f028c),
    (0x0649728e,0x064972aa),(0x06497310,0x06497360),
    (0x06496948,0x064969ae),(0x06497a54,0x06497a74),
    (0x0648b728,0x0648b7c4),(0x0649fa4c,0x0649fa8a),(0x0649fa90,0x0649fae4),
    (0x06481228,0x06481292),(0x06481734,0x06481742),
    (0x06508394,0x065083a8),
]
lines = ['Original Thumb instruction windows. Literal values are annotated; windows may include inline data after returns.']
for start,end in ranges:
    lines.append('\nOriginal bytes at '+hex(start))
    for instruction in cs.disasm(program[start-base:end-base],start):
        text = f'{instruction.address:08x} {instruction.bytes.hex():10} {instruction.mnemonic:8} {instruction.op_str}'
        operands = instruction.operands
        if (instruction.mnemonic == 'ldr' and len(operands)==2
                and operands[1].type == capstone.CS_OP_MEM
                and instruction.reg_name(operands[1].mem.base) == 'pc'):
            literal = ((instruction.address+4)&~3)+operands[1].mem.disp
            text += f' ; [{literal:08x}]={word(literal):08x}'
        lines.append(text)
(HERE/'display-clock-evidence.disasm.txt').write_text('\n'.join(lines)+'\n')
print(json.dumps({'lcd_state':observed['lcd_transition']['state'],
                  'completion':observed['avio_completion']['value'],
                  'gic_irqs':[entry['gic_id']for entry in irq_evidence],
                  'trace_records':len(trace_records),'timing_model_implemented':False}))
