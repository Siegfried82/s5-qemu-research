#!/usr/bin/env python3
"""Test opt-in ADC bank0 ch5 (0x7f030028) model through original S5 decoder 0x065454d8."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from probe.qemu_arm import QemuArm

EXPECTED_PROGRAM_SHA = "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    binary = ROOT / "toolchain/build/qemu-system-arm"
    program = ROOT.parent / "s5-v29-offline/unpacked-v29/03_program.bin"
    program_sha = hashlib.sha256(program.read_bytes()).hexdigest()
    assert program_sha == EXPECTED_PROGRAM_SHA

    report = {
        "scope": "Hardware ADC bank 0 channel 5 MMIO conversion + original Thumb-2 decoder 0x065454d8; no firmware patch or UI write.",
        "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "program_sha256": program_sha,
        "original_instructions_modified": False,
        "guest_ui_or_event_writes": False,
        "checks": [],
    }

    with QemuArm(binary, args.output / "vm", machine="s5-rtos", smp=4,
                 ram="512M", loads=[(program, 0x06280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        def adc_state():
            return json.loads(q.qmp.command("qom-get", {"path": "/machine", "property": "adc-state-json"}))

        def set_prop(name, value):
            q.qmp.command("qom-set", {"path": "/machine", "property": name, "value": value})

        def get_prop(name):
            return q.qmp.command("qom-get", {"path": "/machine", "property": name})

        # Tiny ARM MMIO helpers at 0x10000:
        #   0x10000: ldr r0, [r0]; bx lr
        #   0x10008: str r1, [r0]; bx lr
        q.select_cpu(0)
        q.write_mem(0x10000, struct.pack("<4I", 0xe5900000, 0xe12fff1e, 0xe5801000, 0xe12fff1e))
        q.breakpoint_add(0x10010, kind=4)

        def call(entry, r0=0, r1=0, thumb=True):
            q.select_cpu(0)
            for reg, val in [("cpsr", 0x1f3 if thumb else 0x1d3),
                             ("sp", 0x25000), ("lr", 0x10010),
                             ("r0", r0), ("r1", r1), ("pc", entry)]:
                q.write_reg(reg, val)
            q.resume_cpu(0, timeout=3)
            assert q.read_reg("pc") == 0x10010, hex(q.read_reg("pc"))
            return q.read_reg("r0")

        def mmio_read(addr):
            return call(0x10000, addr, 0, thumb=False)

        def mmio_write(addr, val):
            call(0x10008, addr, val, thumb=False)

        def convert_bank(bank, mask):
            base = 0x7f030000 + bank * 0x1000
            mmio_write(base + 0x0c, mask)
            mmio_write(base + 0x08, bin(mask).count("1"))
            mmio_write(base + 0x04, 0)
            mmio_write(base + 0x00, 1)
            assert (mmio_read(base + 0x00) & 1) == 0
            assert (mmio_read(base + 0x58) & 1) == 0

        # 1. Default state check (opt-in default OFF = 0)
        assert get_prop("adc0-ch5-sample") == 0
        s0 = adc_state()
        assert s0["adc0_ch5_sample"] == 0 and s0["adc11_sample"] == 0 and s0["adc2_ch0_sample"] == 0
        report["checks"].append("default adc0-ch5-sample is 0 and reported in adc-state-json")

        # 2. Range validation rejects values > 0xffff
        rejected = False
        try:
            set_prop("adc0-ch5-sample", 0x10000)
        except RuntimeError:
            rejected = True
        assert rejected, "adc0-ch5-sample must reject > 0xffff"
        assert get_prop("adc0-ch5-sample") == 0
        report["checks"].append("adc0-ch5-sample rejects > 0xffff without mutating stored value")

        # 3. Default conversion (0) -> original 0x065454d8 decodes raw 0x2 (JOYSTICK_DOWN)
        q.write_mem(0x08aa8190 + 0x5c, struct.pack("<I", 0))
        convert_bank(0, 0xfff)
        raw_default = mmio_read(0x7f030028)
        assert raw_default == 0
        decoded_default = call(0x065454d8, raw_default, 0, thumb=True)
        assert decoded_default == 0x2, hex(decoded_default)
        report["checks"].append("default bank0 ch5=0 -> original 0x065454d8 returns raw 0x2 (JOYSTICK_DOWN)")

        # 4. Opt-in adc0-ch5-sample across every resistor-ladder band + idle 0x3ff
        expected_cases = [
            (0x000, 0x02, "BAND0_RAW_0x02_DOWN"),
            (0x0b8, 0x04, "BAND1_RAW_0x04"),
            (0x183, 0x08, "BAND2_RAW_0x08"),
            (0x25a, 0x01, "BAND3_RAW_0x01"),
            (0x322, 0x10, "BAND4_RAW_0x10"),
            (0x3ff, 0x00, "IDLE_NO_KEY"),
        ]
        observed_cases = []
        for sample, expected_raw, label in expected_cases:
            set_prop("adc0-ch5-sample", sample)
            assert get_prop("adc0-ch5-sample") == sample
            convert_bank(0, 0xfff)
            got_b0 = [mmio_read(0x7f030014 + ch * 4) for ch in range(12)]
            expected_b0 = [sample if ch == 5 else 0 for ch in range(12)]
            assert got_b0 == expected_b0, f"{label}: {got_b0}"
            q.write_mem(0x08aa8190 + 0x5c, struct.pack("<I", 0))
            decoded = call(0x065454d8, got_b0[5], 0, thumb=True)
            assert decoded == expected_raw, f"{label}: got {hex(decoded)}, expected {hex(expected_raw)}"
            observed_cases.append({"sample": hex(sample), "mmio_bank0_ch5": got_b0[5],
                                   "decoded_raw_065454d8": hex(decoded), "label": label})
        report["decoder_cases"] = observed_cases
        report["checks"].append("all 5 joystick bands + idle 0x3ff verified through 0x7f030028 and original 0x065454d8")

        # 5. Confirm isolation across bank0 ch5, bank0 ch11, bank1, bank2 ch0
        set_prop("adc0-ch5-sample", 0x3ff)
        set_prop("adc11-sample", 0x300)
        set_prop("adc2-ch0-sample", 0x3fe)
        convert_bank(0, 0xfff)
        convert_bank(1, 0x7f)
        convert_bank(2, 0xf)
        b0 = [mmio_read(0x7f030014 + ch * 4) for ch in range(12)]
        b1 = [mmio_read(0x7f031014 + ch * 4) for ch in range(7)]
        b2 = [mmio_read(0x7f032014 + ch * 4) for ch in range(4)]
        assert b0 == [0, 0, 0, 0, 0, 0x3ff, 0, 0, 0, 0, 0, 0x300]
        assert b1 == [0] * 7
        assert b2 == [0x3fe, 0, 0, 0]
        final_state = adc_state()
        assert final_state["adc0_ch5_sample"] == 0x3ff
        assert final_state["adc11_sample"] == 0x300
        assert final_state["adc2_ch0_sample"] == 0x3fe
        assert final_state["banks"][0]["results"] == b0
        assert final_state["banks"][1]["results"] == b1
        assert final_state["banks"][2]["results"] == b2
        assert all(bank["unknown_reads"] == 0 and bank["unknown_writes"] == 0 for bank in final_state["banks"])
        report["checks"].append("bank0 ch5 / bank0 ch11 / bank1 / bank2 ch0 isolation verified")
        report["final_adc_state"] = final_state

    report["passed"] = True
    (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": len(report["checks"]),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
