#!/usr/bin/env python3
"""Test opt-in ADC bank2 ch0 (0x7f032014) model through original S5 decoder 0x06545554."""
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
        "scope": "Hardware ADC bank 2 channel 0 MMIO conversion + original Thumb-2 decoder 0x06545554; no firmware patch or UI write.",
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
            mmio_write(base + 0x0c, mask)  # mask
            mmio_write(base + 0x08, bin(mask).count("1"))  # count
            mmio_write(base + 0x04, 0)     # scan
            mmio_write(base + 0x00, 1)     # start conversion
            assert (mmio_read(base + 0x00) & 1) == 0
            assert (mmio_read(base + 0x58) & 1) == 0

        # 1. Default state check (opt-in default OFF = 0)
        assert get_prop("adc2-ch0-sample") == 0
        s0 = adc_state()
        assert s0["adc2_ch0_sample"] == 0 and s0["adc11_sample"] == 0
        report["checks"].append("default adc2-ch0-sample is 0 and reported in adc-state-json")

        # 2. Range validation rejects values > 0xffff
        rejected = False
        try:
            set_prop("adc2-ch0-sample", 0x10000)
        except RuntimeError:
            rejected = True
        assert rejected, "adc2-ch0-sample must reject > 0xffff"
        assert get_prop("adc2-ch0-sample") == 0
        report["checks"].append("adc2-ch0-sample rejects > 0xffff without mutating stored value")

        # 3. Default conversion (0) -> original 0x06545554 decodes raw 0x20 (KEY_LEFT)
        q.write_mem(0x08aa8190 + 0x58, struct.pack("<I", 0))
        convert_bank(2, 0xf)
        raw_default = mmio_read(0x7f032014)
        assert raw_default == 0
        assert [mmio_read(0x7f032014 + ch * 4) for ch in range(4)] == [0, 0, 0, 0]
        decoded_default = call(0x06545554, raw_default, 0, thumb=True)
        assert decoded_default == 0x20, hex(decoded_default)
        report["checks"].append("default bank2 ch0=0 -> original 0x06545554 returns raw 0x20 (KEY_LEFT)")

        # 4. Opt-in adc2-ch0-sample across every resistor-ladder band + idle 0x3ff
        expected_cases = [
            (0x000, 0x20, "KEY_LEFT"),
            (0x0b8, 0x04, "KEY_UP"),
            (0x183, 0x10, "KEY_RIGHT"),
            (0x25a, 0x08, "KEY_DOWN"),
            (0x3ff, 0x00, "IDLE_NO_KEY"),
        ]
        observed_cases = []
        for sample, expected_raw, label in expected_cases:
            set_prop("adc2-ch0-sample", sample)
            assert get_prop("adc2-ch0-sample") == sample
            convert_bank(2, 0xf)
            got_mmio = [mmio_read(0x7f032014 + ch * 4) for ch in range(4)]
            assert got_mmio == [sample, 0, 0, 0], f"{label}: {got_mmio}"
            # Reset latched decoder word [0x08aa8190 + 0x58] and invoke original 0x06545554(sample, 0)
            q.write_mem(0x08aa8190 + 0x58, struct.pack("<I", 0))
            decoded = call(0x06545554, got_mmio[0], 0, thumb=True)
            assert decoded == expected_raw, f"{label}: got {hex(decoded)}, expected {hex(expected_raw)}"
            observed_cases.append({"sample": hex(sample), "mmio_bank2": got_mmio,
                                   "decoded_raw_06545554": hex(decoded), "label": label})
        report["decoder_cases"] = observed_cases
        report["checks"].append("all 4 directional bands + idle 0x3ff verified through 0x7f032014 and original 0x06545554")

        # 5. Confirm isolation: bank0 ch11 (adc11-sample) and bank1 stay independent
        set_prop("adc11-sample", 0x300)
        set_prop("adc2-ch0-sample", 0x3ff)
        convert_bank(0, 0xfff)
        convert_bank(1, 0x7f)
        convert_bank(2, 0xf)
        b0 = [mmio_read(0x7f030014 + ch * 4) for ch in range(12)]
        b1 = [mmio_read(0x7f031014 + ch * 4) for ch in range(7)]
        b2 = [mmio_read(0x7f032014 + ch * 4) for ch in range(4)]
        assert b0 == [0] * 11 + [0x300]
        assert b1 == [0] * 7
        assert b2 == [0x3ff, 0, 0, 0]
        final_state = adc_state()
        assert final_state["adc11_sample"] == 0x300
        assert final_state["adc2_ch0_sample"] == 0x3ff
        assert final_state["banks"][0]["results"] == b0
        assert final_state["banks"][1]["results"] == b1
        assert final_state["banks"][2]["results"] == b2
        assert all(bank["unknown_reads"] == 0 and bank["unknown_writes"] == 0 for bank in final_state["banks"])
        report["checks"].append("bank0/bank1/bank2 isolation and adc-state-json accounting verified")
        report["final_adc_state"] = final_state

    report["passed"] = True
    (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": len(report["checks"]),
                      "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
