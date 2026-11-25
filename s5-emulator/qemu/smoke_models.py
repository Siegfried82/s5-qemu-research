#!/usr/bin/env python3
"""Bounded synthetic guest checks for the modeled SPI and module protocols."""
import json
from pathlib import Path
import struct
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'probe'))
from qemu_arm import QemuArm
from generate_board_setup import code_from_object


def main():
    out = HERE / 'results-models-smoke'
    out.mkdir(exist_ok=True)
    source = out / 'spi-transfer.S'
    source.write_text('''\
.syntax unified
.arm
.text
.global _start
_start:
    movw r4, #0x01c7
    strh r4, [r2]
    mov r4, #2
    strh r4, [r2, #0x10]
    strh r4, [r2, #4]
    movw r4, #1
    movt r4, #0x1000
    str r4, [r3]
    lsr r5, r0, #8
    strh r5, [r2, #8]
    strh r0, [r2, #8]
    strh r1, [r2, #8]
    ldrh r5, [r2, #8]
    ldrh r5, [r2, #8]
    ldrh r0, [r2, #8]
    sub r4, r4, #1
    str r4, [r3]
    bx lr
''')
    obj = out / 'spi-transfer.o'
    subprocess.run(['clang', '--target=arm-none-eabi', '-march=armv7-a',
                    '-marm', '-c', str(source), '-o', str(obj)], check=True)
    code = code_from_object(obj.read_bytes())
    result = {'kind': 'synthetic-peripheral-smoke', 'firmware_executed': False}
    with QemuArm(HERE.parent / 'toolchain/build/qemu-system-arm', out,
                 ram='512M', machine='s5-rtos', smp=4,
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000),
                                 (0x80000000, 0x20000000)]) as emu:
        emu.select_cpu(0)
        emu.write_mem(0x11000, code)
        emu.breakpoint_add(0x11100, 4)

        def transfer(command, data=0xff):
            for register, value in [('cpsr', 0x1d3), ('pc', 0x11000),
                                    ('lr', 0x11100), ('r0', command),
                                    ('r1', data), ('r2', 0x7f043000),
                                    ('r3', 0x7f081c0c)]:
                emu.write_reg(register, value)
            emu.resume_cpu(0, timeout=2)
            emu.select_cpu(0)
            assert emu.read_reg('pc') == 0x11100
            return emu.read_reg('r0')

        assert transfer(0x040f) == 1
        assert transfer(0x0411) == 0
        assert transfer(0x0439) == 0
        transfer(0x0438, 0x55)
        assert transfer(0x0439) == 0x55
        assert transfer(0x041b) == 0
        result['rtc'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine/rtc-peer', 'property': 'state-json'}))
        assert result['rtc']['reads'] == 5
        assert result['rtc']['writes'] == 1
        assert result['rtc']['unknown_reads'] == 1
        assert result['rtc']['unsupported_registers'] == ['0x041a']
        assert result['rtc']['malformed_frames'] == 0

        # GDB debug MMIO accesses can be split into bytes.  Exercise registers
        # with real guest STR/LDR so the model sees the supported 32-bit width.
        emu.write_mem(0x12000, struct.pack('<3I', 0xe5801000, 0xe5900000, 0xe12fff1e))
        emu.breakpoint_add(0x12100, 4)

        def reg32(address, value):
            for register, setting in [('cpsr', 0x1d3), ('pc', 0x12000),
                                      ('lr', 0x12100), ('r0', address),
                                      ('r1', value)]:
                emu.write_reg(register, setting)
            emu.resume_cpu(0, timeout=2)
            emu.select_cpu(0)
            return emu.read_reg('r0')

        assert reg32(0x7c160000, 0x30) == 0x33
        assert reg32(0x7c160000, 0) == 0
        assert reg32(0x7c160098, 0xf0) == 0xff
        assert reg32(0x7c1600c0, 1) == 0x111
        assert reg32(0x7c1600c0, 0) == 0
        assert reg32(0x7c160124, 1) == 0x111
        assert reg32(0x7c160008, 0x12345678) == 0x12345678
        result['modules'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'module-state-json'}))
        assert result['modules']['requests'] == 6
        assert result['modules']['unknown_writes'] == 1
        assert result['modules']['unknown_reads'] == 1
        assert reg32(0x70028958, 0x12345678) == 0x12345678
        assert reg32(0x7002a3f0, 0x98765432) == 0x98765432
        assert reg32(0x7002bffc, 0xa5a5a5a5) == 0xa5a5a5a5
        result['pool_sram_readback'] = True
        assert reg32(0x7b000000, 0x12345678) == 0x12345678
        assert reg32(0x7b020000, 0x87654321) == 0x87654321
        assert reg32(0x7b02fffc, 0xa55aa55a) == 0xa55aa55a
        result['coprocessor_upload_storage_only'] = True
        emu.write_mem(0x13000, struct.pack('<5I', 0xe3a01a01, 0xe2511001,
                                          0x1afffffd, 0xe5900000, 0xe12fff1e))
        emu.write_mem(0x14000, struct.pack('<2I', 0xe5900000, 0xe12fff1e))
        emu.breakpoint_add(0x14100, 4)

        def timer_value(address, spin=False):
            for register, value in [('cpsr', 0x1d3),
                                    ('pc', 0x13000 if spin else 0x14000),
                                    ('lr', 0x14100), ('r0', address)]:
                emu.write_reg(register, value)
            emu.resume_cpu(0, timeout=2)
            emu.select_cpu(0)
            return emu.read_reg('r0')

        result['timers'] = []
        for group in range(13):
            for offset in (0, 0x20):
                base = 0x7f020000 + group * 0x1000 + offset
                reg32(base, 0x1000000)
                reg32(base + 8, 0xc2)
                before = timer_value(base + 4)
                after = timer_value(base + 4, spin=True)
                assert 0 < after < before <= 0x1000000, (hex(base), before, after)
                result['timers'].append({'base': hex(base), 'before': before,
                                         'after': after})
                reg32(base + 8, 0)
                reg32(base + 0xc, 1)
        result['timer_irq_map'] = []
        for index in range(26):
            base = 0x7f020000 + (index // 2) * 0x1000 + (index % 2) * 0x20
            reg32(base, 20)
            reg32(base + 8, 0xa3)
            expected = 1 << (index + 4)
            pending = timer_value(0x70001204, spin=True)
            for retry in range(20):
                if pending == expected:
                    break
                pending = timer_value(0x70001204, spin=True)
            assert pending == expected, (hex(base), hex(pending), hex(expected))
            result['timer_irq_map'].append({'base': hex(base), 'gic_id': index + 36,
                                            'pending_word': hex(pending)})
            reg32(base + 8, 0)
            reg32(base + 0xc, 1)
            assert timer_value(0x70001204) == 0
        result['gic'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'gic-state-json'}))
        assert len(result['gic']['cpus']) == 4
        assert emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'inactive-inputs'}) is True
        assert timer_value(0x7f080094) == 0x10
        assert timer_value(0x7f080410) == 0x10
        # Also verify the original driver's 16-bit access width.
        emu.write_mem(0x15000, struct.pack('<2I', 0xe1d000b0, 0xe12fff1e))
        for address in (0x7f080094, 0x7f080410):
            for register, value in [('cpsr', 0x1d3), ('pc', 0x15000),
                                    ('lr', 0x14100), ('r0', address)]:
                emu.write_reg(register, value)
            emu.resume_cpu(0, timeout=2)
            emu.select_cpu(0)
            assert emu.read_reg('r0') == 0x10
        emu.qmp.command('qom-set', {'path': '/machine',
                                   'property': 'inactive-inputs', 'value': False})
        assert timer_value(0x7f080094) == 0
        assert timer_value(0x7f080410) == 0
        result['gpio_input_profile_16_and_32_bit_accesses'] = True
        for cpu in range(4):
            base = 0x70002000 + cpu * 0x8000
            reg32(base, 1)
            reg32(base + 4, 0xa0 + 0x10 * cpu)
        result['gic_fixed_cpu_views'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'gic-state-json'}))['cpus']
        assert [c['control'] for c in result['gic_fixed_cpu_views']] == [1] * 4
        assert [c['priority_mask'] for c in result['gic_fixed_cpu_views']] == [0xa0, 0xb0, 0xc0, 0xd0]
        reg32(0x70001000, 1)
        assert [timer_value(0x70001000 + cpu * 0x8000) for cpu in range(4)] == [1] * 4
        reg32(0x70001000, 0)
        # The strict SPI0 gate stops on TX before the PL022 can invent RX.
        emu.write_mem(0x16000, struct.pack('<2I', 0xe5801000, 0xe12fff1e))
        for register, value in [('cpsr', 0x1d3), ('pc', 0x16000),
                                ('lr', 0x14100), ('r0', 0x7f040008), ('r1', 0xf5)]:
            emu.write_reg(register, value)
        stop = emu.resume_cpu(0, timeout=2)
        emu.select_cpu(0)
        result['spi0_strict'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'spi0-state-json'}))
        assert result['spi0_strict']['strict_stops'] == 1
        assert result['spi0_strict']['data_accesses'] == 1
        assert result['spi0_strict']['last_access']['value'] == 0xf5
        assert result['spi0_strict']['open_bus_rx_reads'] == 0
        assert timer_value(0x7f04000c) & 4 == 0
        emu.qmp.command('qom-set', {'path': '/machine',
                                   'property': 'spi0-open-bus', 'value': True})
        reg32(0x7f040000, 0x01c7)
        reg32(0x7f040004, 2)
        reg32(0x7f040008, 0xf5)
        result['spi0_open_bus'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'spi0-state-json'}))
        assert result['spi0_open_bus']['profile'] == 'absent-peer-assume-MISO-low'
        assert result['spi0_open_bus']['open_bus_rx_reads'] == 1
        assert result['spi0_open_bus']['last_access']['value'] == 0
        for channel in (1, 2, 4, 5, 6):
            base = 0x7f040000 + channel * 0x1000
            for register, value in [('cpsr', 0x1d3), ('pc', 0x16000),
                                    ('lr', 0x14100), ('r0', base + 8),
                                    ('r1', 0x50 + channel)]:
                emu.write_reg(register, value)
            emu.resume_cpu(0, timeout=2)
            emu.select_cpu(0)
            assert timer_value(base + 0xc) & 4 == 0
        result['spi_all_strict'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'spi-state-json'}))
        for channel in (1, 2, 4, 5, 6):
            state = result['spi_all_strict']['channels'][channel]
            assert state['strict_stops'] == 1
            assert state['data_accesses'] == 1
            assert state['open_bus_rx_reads'] == 0
        emu.qmp.command('qom-set', {'path': '/machine',
                                   'property': 'spi-open-bus-mask', 'value': 0x77})
        assert emu.qmp.command('qom-get', {'path': '/machine',
                                          'property': 'spi6-open-bus'}) is True
        for channel in (1, 2, 4, 5, 6):
            base = 0x7f040000 + channel * 0x1000
            reg32(base, 0x1c7)
            reg32(base + 4, 2)
            assert reg32(base + 8, 0x50 + channel) == 0
        result['spi6_open_bus'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'spi6-state-json'}))
        assert result['spi6_open_bus']['channel'] == 6
        assert result['spi6_open_bus']['open_bus_rx_reads'] == 1
        result['uarts'] = []
        for channel, irq in enumerate((85, 86, 294, 295, 296)):
            base = 0x7f050000 + channel * 0x1000
            assert timer_value(base + 0x18) & 0x10
            reg32(base + 0x38, 0x20)
            reg32(base, ord('U'))
            text = emu.qmp.command('ringbuf-read', {
                'device': 's5-uart%d' % channel, 'size': 16, 'format': 'utf8'})
            assert text == 'U', (channel, text)
            irq_state = json.loads(emu.qmp.command('qom-get', {
                'path': '/machine', 'property': 'gic-state-json'}))
            asserted = [entry['id'] for entry in irq_state['enabled_or_asserted_irqs']
                        if entry['level']]
            assert asserted == [irq], (channel, asserted, irq)
            reg32(base + 0x38, 0)
            result['uarts'].append({'channel': channel, 'gic_id': irq,
                                     'tx_capture': text, 'empty_rx_flag': True})
        assert emu.qmp.command('qom-get', {'path': '/machine',
                                          'property': 'adc11-sample'}) == 0
        reg32(0x7f03000c, 1 << 11)
        reg32(0x7f030008, 1)
        reg32(0x7f030000, 1)
        assert timer_value(0x7f030040) == 0
        emu.qmp.command('qom-set', {'path': '/machine',
                                   'property': 'adc11-sample', 'value': 0x300})
        assert timer_value(0x7f030040) == 0  # external input is not a result write
        assert reg32(0x7f030000, 0x21) == 0x20
        assert timer_value(0x7f030040) == 0x300
        assert timer_value(0x7f030058) & 1 == 0
        for bank, channels in ((1, 7), (2, 4)):
            base = 0x7f030000 + bank * 0x1000
            reg32(base + 0xc, (1 << channels) - 1)
            reg32(base + 8, 1)
            assert reg32(base, 1) == 0
            assert timer_value(base + 0x14 + (channels - 1) * 4) == 0
        result['adc'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'adc-state-json'}))
        assert result['adc']['banks'][0]['conversions'] == 2
        assert result['adc']['banks'][0]['results'][11] == 0x300
        # Original S5 bit-4 mode: repeated timer expiries must produce new
        # edges even though the firmware never writes the timer INTCLR.
        reg32(0x70001824, 0x01010101)  # IDs 36..39 target CPU0
        reg32(0x70001c08, 0x200)      # ID36 edge triggered, as original loader
        reg32(0x7f020000, 27000)      # 1 ms at 27 MHz
        reg32(0x7f020008, 0xf2)
        result['timer_bit4_pulse_periods_without_intclr'] = []
        for period in range(5):
            reg32(0x70001284, 1 << 4)  # consume previous GIC pending state only
            for retry in range(1000):
                pending = timer_value(0x70001204, spin=True)
                if pending & (1 << 4):
                    break
            assert pending & (1 << 4), ('missing periodic edge', period)
            result['timer_bit4_pulse_periods_without_intclr'].append(period + 1)
        assert timer_value(0x7f020010) == 1  # RIS remains truthfully latched
        assert timer_value(0x7f020014) == 1  # MIS remains truthfully latched
        reg32(0x7f020008, 0)
        result['i2c_error_irq_map'] = []
        for bus in range(5):
            base = 0x7f058000 + bus * 0x100
            reg32(base, 0x120)  # preserve the original controller's bit 8
            reg32(base + 4, 0x10)
            reg32(base + 0xc, 0x30)
            assert reg32(base + 4, 0xf0) == 0xf8
            assert timer_value(base) == 0x130
            state = json.loads(emu.qmp.command('qom-get', {
                'path': '/machine', 'property': 'gic-state-json'}))
            asserted = [entry['id'] for entry in state['enabled_or_asserted_irqs']
                        if entry['level']]
            assert asserted == [282 + bus], (bus, asserted)
            reg32(base, 0x100)
            reg32(base + 4, 0)
            result['i2c_error_irq_map'].append({'bus': bus, 'gic_id': 282 + bus})
        # Pending completion is retained while IRQ enable is clear.
        reg32(0x7f058000, 0x100)
        assert reg32(0x7f058004, 0xb0) == 0xb8
        assert timer_value(0x7f058000) == 0x110
        reg32(0x7f058000, 0x130)
        result['i2c'] = json.loads(emu.qmp.command('qom-get', {
            'path': '/machine', 'property': 'i2c-state-json'}))
        assert result['i2c']['buses'][0]['starts'] == 2
        assert result['i2c']['buses'][0]['errors'] == 2
        assert result['i2c']['buses'][0]['irq_assertions'] == 2
        assert result['i2c']['successful_peer_replies'] == 0
        reg32(0x7f058000, 0x100)
        reg32(0x7f058004, 0)
    result['passed'] = True
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
