#!/usr/bin/env python3
"""Extract original LCD SPI6 setup from the saved local scheduling experiment."""
from pathlib import Path
import argparse
import hashlib
import json
import mmap
import struct
import capstone

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--snapshot', type=Path, default=ROOT / 's5-emulator/menu-smp/results/isolated-display-first')
args = parser.parse_args()
program = (ROOT / 's5-v29-offline/unpacked-v29/03_program.bin').read_bytes()
base = 0x06280000
sha = hashlib.sha256(program).hexdigest()
assert sha == 'fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe'
boot = json.loads((args.snapshot / 'result.json').read_text())
with (args.snapshot / 'ram.bin').open('rb') as f:
    ram = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    u32 = lambda a: struct.unpack_from('<I', ram, a)[0]
    u16 = lambda a: struct.unpack_from('<H', ram, a)[0]
    tcb = u32(0x20040)
    regs = boot['final_registers'][0]
    tx = int(regs['r1'], 0)
    count = int(regs['r3'], 0)
    table = []
    for a in range(0x08a9c8c8, 0x08a9c8ec, 4):
        source = u32(a)
        if source == 0:
            break
        word = u16(source)
        table.append({'pointer_slot': hex(a), 'source': hex(source), 'word': hex(word),
                      'tx': bytes([((word >> 8) & 0x7f) << 1, word & 0xff]).hex()})
    stack_slots = [0x099ce2ec, 0x099ce2f4, 0x099ce324, 0x099ce344,
                   0x099ce354, 0x099ce364, 0x099cea4c]
    result = {
        'scope': 'Local scheduling experiment: original task66 temporarily delayed through original OS API; not a completed original boot',
        'snapshot': str(args.snapshot / 'ram.bin'),
        'program_sha256': sha,
        'original_firmware_instructions_modified': boot['original_firmware_instructions_modified'],
        'display_isolation': boot['display_isolation'],
        'original_execution_events': [e['label'] for e in boot['events']],
        'current_task': {'tcb': hex(tcb), 'id': u32(tcb + 0xc0),
                         'name': ram[tcb+0xc4:tcb+0xd4].split(b'\0')[0].decode(),
                         'entry': hex(u32(tcb+0xd8)), 'priority': u32(tcb+0x2c),
                         'run_count': u32(tcb+4), 'state': u32(tcb+0xec)},
        'registers': regs,
        'wait': {'controller': '0x7f046000', 'driver': '0x0642c43c',
                 'pc': regs['r15'], 'condition': 'SSPSR +0xc bit0 (TFE); no byte emitted yet at snapshot',
                 'tx_pointer': hex(tx), 'tx_hex': ram[tx:tx+count].hex(),
                 'transfer_bytes': count},
        'stack_return_slots': {hex(a): hex(u32(a)) for a in stack_slots},
        'caller_chain': [
            {'entry': '0x0647e83e', 'role': 'uiTSK018 original AVIO task'},
            {'entry': '0x0647e6f8', 'role': 'initialization wrapper'},
            {'entry': '0x0647e0e2', 'role': 'avio_control_lcdinit_static; original string at0x0647e48c'},
            {'entry': '0x064840ee', 'role': 'avio_lcdevf_driver_serial_sendall(lcdevf=0); original string at0x064844bc'},
            {'entry': '0x06484de2', 'role': 'lcdevf=0 choosesLCD function0x06483aee;1 choosesEVF function0x06484f18'},
            {'entry': '0x06483aee', 'role': 'iterates original16bit register/value words, two independent one-byte transfers perword'},
            {'entry': '0x06484dd4', 'role': 'lcdevf=0 selectsSPI6;1 selectsSPI4'},
            {'entry': '0x064e6a24', 'role': 'single-byte full-duplex transfer through0x0642327e to0x0642c43c'},
        ],
        'lcd_protocol_observed_in_original_code': {
            'frame': 'For each u16 word: byte0=((word>>8)&0x7f)<<1, byte1=word&0xff',
            'cs_function': '0x06484db6', 'cs_encoded_register': '0x7f083018',
            'select_write': '0x10000000', 'deselect_write': '0x10000001',
            'physical_cs_polarity_verified': False,
            'receive_semantics': 'Single-byte helper reads RX and returns it, but LCD caller0x06483aee ignores both results; no ACK/success check in this path',
            'evf_distinction': 'SPI4/CS7f082818 has a separate path with read mode at6484f98; SPI6 conclusions do not establishEVF response semantics',
            'first_eight_writes': table,
            'table_source': {'component': '14_eep_fix.bin', 'offset': '0x1a000',
                             'resident_address': '0x0631a000', 'identical_program_offset': '0x9a000'},
        },
        'limitations': ['No physicalLCD part identified', 'No peripheral ACK fabricated',
                        'No framebuffer or menu rendered in this snapshot',
                        'Controller-only continuation does not emulate panel scanout'],
    }
    eep = (ROOT / 's5-v29-offline/unpacked-v29/14_eep_fix.bin').read_bytes()
    assert ram[0x0631a000:0x0631a040] == eep[0x1a000:0x1a040]
    assert result['current_task']['id'] == 18
    assert result['wait']['tx_hex'] == '60'
    assert table[0]['tx'] == '6000'
    (HERE / 'lcd-spi-wait-evidence.json').write_text(json.dumps(result, indent=2)+'\n')

md = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
lines = [f'Original program sha256={sha}; Thumb instructions; source words are not modified.']
for a, n in [(0x647e83e,0x1c),(0x647e6f8,0xc),(0x647e0e2,0x13c),
             (0x64840ee,0x34),(0x6484de2,0x26),(0x6483aee,0x7e),
             (0x6484db6,0x2c),(0x64e6a24,0x30),(0x642327e,0x30),
             (0x642c43c,0xe0),(0x6484f18,0x9a)]:
    lines.append('\nRANGE '+hex(a))
    for i in md.disasm(program[a-base:a-base+n], a):
        lines.append(f'{i.address:08x}: {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}')
(HERE / 'lcd-spi-wait.disasm.txt').write_text('\n'.join(lines)+'\n')
print('Saved LCD SPI6 evidence and original disassembly; task/source/TX assertions passed.')
