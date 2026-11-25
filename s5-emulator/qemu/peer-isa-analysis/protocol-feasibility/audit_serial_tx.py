#!/usr/bin/env python3
"""Read-only reconstruction of the 7b1934xx transmit/poll contract."""
import hashlib
import json
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from audit_ui_service import HERE, EMU, PROGRAM, BASE, EXPECTED_SHA


def main():
    p = PROGRAM.read_bytes()
    assert hashlib.sha256(p).hexdigest() == EXPECTED_SHA
    u = lambda a: struct.unpack_from('<I', p, a - BASE)[0]
    assert u(0x67095f0) == 0x7b193408
    assert u(0x67095f4) == 0x01020002
    count, records = u(0x6fc2f1c), u(0x6fc2f20)
    assert count == 157 and records == 0x6fc1fb4
    table = []
    for i in range(count):
        address, data, padding = struct.unpack_from('<HBB', p, records - BASE + i * 4)
        packet = bytes([0x81, address >> 8, address & 255, data])
        table.append({'index': i, 'record': hex(records + i * 4),
                      'address': hex(address), 'data': data, 'unused_padding': padding,
                      'tx_bytes_hex': packet.hex(), 'tx_word': hex(int.from_bytes(packet, 'little'))})
    assert table[144]['tx_bytes_hex'] == '81078d00'
    ranges = [
        ('single record transfer setup', 0x67091f2, 0x67092a2),
        ('single record packing and bounded polls', 0x67092a2, 0x6709314),
        ('packed up-to-32-byte transfer', 0x6709314, 0x67093de),
        ('contiguous record batching', 0x67093de, 0x67094e4),
        ('staged record construction', 0x67094f8, 0x670951e),
    ]
    lines = [f'Program SHA256 {EXPECTED_SHA}; selected function ranges, no guest execution.']
    for label, start, end in ranges:
        c = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
        c.detail = True
        lines.append(f'\n{label}: {start:#x}..{end:#x}')
        for i in c.disasm(p[start - BASE:end - BASE], start):
            note = ''
            if i.mnemonic.startswith('ldr') and '[pc,' in i.op_str:
                literal = ((i.address + 4) & ~3) + i.operands[-1].mem.disp
                note = f' ; [{literal:#010x}]={u(literal):#010x}'
            lines.append(f'{i.address:08x} {i.bytes.hex():10} {i.mnemonic:9} {i.op_str}{note}')
    (HERE / 'serial-tx-disassembly.txt').write_text('\n'.join(lines) + '\n')
    saved = json.loads((EMU / 'menu-smp/results/local-frame-paced-menu-stepfix/result.json').read_text())
    regs = saved['final_registers'][0]
    assert int(regs['r2'], 16) == 144 and int(regs['r5'], 16) == count
    evidence = {
        'scope': 'Offline original-code and saved-stop audit; no controller implemented or ready written',
        'program_sha256': EXPECTED_SHA,
        'source': str(PROGRAM),
        'source_functions': {'single_records': '0x067091f2', 'packed_transfer': '0x06709314',
                             'contiguous_batching': '0x067093de'},
        'registers': [
            {'address': '0x7b193400', 'observed_operation': 'write', 'value': '0x01020002',
             'meaning': 'configuration value; physical field names unverified'},
            {'address': '0x7b193404', 'observed_operation': 'write', 'value': '0x300 | byte_count',
             'meaning': 'low6 bits hold outgoing byte count in examined paths'},
            {'address': '0x7b193408', 'observed_operation': 'write', 'values': [0, 1],
             'meaning': '1 after TX buffer setup; 0 after completion/timeout'},
            {'address': '0x7b19340c', 'observed_operation': 'write',
             'value': '0xffffffff << byte_count, truncated32; count32=>0',
             'meaning': 'mask derived from count; physical bit meaning unverified'},
            {'address': '0x7b193410..0x7b19342c', 'observed_operation': 'write',
             'meaning': 'up to32 TX bytes packed little-endian into32-bit words'},
            {'address': '0x7b193430', 'observed_operation': 'write', 'values': [0, 16],
             'meaning': 'write16 then poll statusbit8 clear; write0 after clear'},
            {'address': '0x7b19343c', 'observed_operation': 'write', 'values': [0, 1],
             'meaning': 'also touched by original startup sequence; exact hardware role unknown'},
            {'address': '0x7b193444', 'observed_operation': 'read',
             'meaning': 'pollbit8 set afterSTART, then clear afterACK; noRXpayload read'},
            {'address': '0x7b193448', 'observed_operation': 'write', 'value': 1,
             'meaning': 'setup writes1; physical role unknown'},
        ],
        'packet': {'known_prefix': '0x81', 'first4_bytes': ['0x81', 'address_hi8', 'address_lo8', 'data8'],
                   'record_layout': 'uint16 address,uint8 data,uint8 unused',
                   'batching': 'Original ARM code adds data for consecutive addresses until totalcount32',
                   'physical_peer_identity': 'unverified', 'peer_ack_or_rx_checked': False},
        'lifecycle': ['clear old completion if bit8 set, usingACK16', 'programconfiguration/count/mask/TX',
                      'START1', 'boundedwait statusbit8=1', 'ACK16', 'boundedwait statusbit8=0',
                      'ACK0', 'START0'],
        'timeout': {'counter_limit': '0x10000', 'not_infinite': True,
                    'single_function': 'set aggregateerror1, continue to nextrecord; returns1 if anytimeout',
                    'packed_function': 'returns1 for timeout, stopsCTRL and zeroesbatchcount'},
        'saved_stop': {'path': 'menu-smp/results/local-frame-paced-menu-stepfix/result.json',
                       'cpu0': regs, 'record_index': 144, 'record_count': count,
                       'interpretation': 'host stage budget interrupted a bounded original poll near batch end; not proven deadlock'},
        'active_table': table,
        'possible_hle_boundary': {
            'implemented': False, 'kind': 'explicit opt-in transmit-only controller completion sink',
            'allowed_claim': 'captured known outgoing bytes reached software sink; completion acknowledged by original code',
            'unsupported_claims': ['physical peer accepted writes', 'sensor exists or works', 'DSP executes', 'frame data generated'],
            'required_guards': ['known configuration and byte_count4..32', 'all requiredTXwords populated',
                                'mask consistent withcount', 'known prefix81', 'START/complete/ACK lifecycle'],
            'unknown_policy': 'capture and stop or leave unsupported; never supply generic success for unknown requests',
            'still_unknown': ['device identity', 'physical transfer rate', 'electrical bus protocol',
                              'error/overrun bits', 'RX/read commands', 'other modes and registers'],
        },
    }
    (HERE / 'serial-tx-contract.json').write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps({'records_verified': count, 'stopped_record': table[144], 'model_implemented': False}, indent=2))


if __name__ == '__main__':
    main()
