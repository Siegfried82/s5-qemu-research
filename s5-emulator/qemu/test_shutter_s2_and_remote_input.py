#!/usr/bin/env python3
"""Test opt-in shutter S2 GPIO (0x7f081038) and ADC1 ch4 (0x7f031024) remote detector."""
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
        "scope": "Hardware shutter S2 GPIO 0x7f081038 + ADC bank1 ch4 0x7f031024 + original Thumb-2 remote detector 0x06547a0e; no firmware patch.",
        "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "program_sha256": program_sha,
        "original_instructions_modified": False,
        "checks": [],
    }

    with QemuArm(binary, args.output / "vm", machine="s5-rtos", smp=4,
                 ram="512M", loads=[(program, 0x06280000)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as q:
        def get_prop(name):
            return q.qmp.command("qom-get", {"path": "/machine", "property": name})

        def set_prop(name, value):
            q.qmp.command("qom-set", {"path": "/machine", "property": name, "value": value})

        def adc_state():
            return json.loads(get_prop("adc-state-json"))

        q.select_cpu(0)
        # 0x10000: ldr r0, [r0]; bx lr
        # 0x10008: str r1, [r0]; bx lr
        # 0x10010: ldrh r0, [r0]; bx lr
        q.write_mem(0x10000, struct.pack("<6I",
                                         0xe5900000, 0xe12fff1e,
                                         0xe5801000, 0xe12fff1e,
                                         0xe1d000b0, 0xe12fff1e))
        q.breakpoint_add(0x10020, kind=4)

        def call(entry, r0=0, r1=0, thumb=True):
            q.select_cpu(0)
            for reg, val in [("cpsr", 0x1f3 if thumb else 0x1d3),
                             ("sp", 0x25000), ("lr", 0x10020),
                             ("r0", r0), ("r1", r1), ("pc", entry)]:
                q.write_reg(reg, val)
            q.resume_cpu(0, timeout=3)
            assert q.read_reg("pc") == 0x10020, hex(q.read_reg("pc"))
            return q.read_reg("r0")

        def mmio_read32(addr):
            return call(0x10000, addr, 0, thumb=False)

        def mmio_read16(addr):
            return call(0x10010, addr, 0, thumb=False)

        def mmio_write32(addr, val):
            call(0x10008, addr, val, thumb=False)

        def convert_bank1():
            mmio_write32(0x7f03100c, 0x7f)
            mmio_write32(0x7f031008, 7)
            mmio_write32(0x7f031004, 0)
            mmio_write32(0x7f031000, 1)

        # 1. Defaults OFF
        assert get_prop("shutter-s2-input-high") is False
        assert get_prop("adc1-ch4-sample") == 0
        assert mmio_read16(0x7f081038) == 0
        assert mmio_read16(0x7f08103c) == 0
        report["checks"].append("defaults shutter-s2-input-high=False and adc1-ch4-sample=0 verified")

        # 2. Opt-in shutter-s2-input-high sets ONLY 0x7f081038 bit 4; 0x7f08103c stays 0
        set_prop("shutter-s2-input-high", True)
        assert get_prop("shutter-s2-input-high") is True
        assert mmio_read16(0x7f081038) == 0x10
        assert mmio_read16(0x7f08103c) == 0
        assert mmio_read16(0x7f08101c) == 0
        report["checks"].append("shutter-s2-input-high sets ONLY 0x7f081038 bit 4 (0x10); neighbor GPIOs remain 0")

        # 3. ADC1 ch4 range check
        rejected = False
        try:
            set_prop("adc1-ch4-sample", 0x10000)
        except RuntimeError:
            rejected = True
        assert rejected
        assert get_prop("adc1-ch4-sample") == 0
        report["checks"].append("adc1-ch4-sample rejects > 0xffff")

        # 4. Run original 0x06547a0e on ADC1 ch4=0 (mode 3 remote plugged) vs 0x3ff (mode 0 unplugged)
        # Clear remote debounce struct at literal of 0x06547a12 (0x06547a74)
        prog_bytes = program.read_bytes()
        remote_struct = struct.unpack_from("<I", prog_bytes, 0x06547a74 - 0x06280000)[0]
        q.write_mem(remote_struct, bytes(32))

        convert_bank1()
        raw0 = mmio_read32(0x7f031024)
        assert raw0 == 0
        # 0x06547a0e debounces over 4 calls (count >= 2)
        for _ in range(4):
            mode_zero = call(0x06547a0e, raw0, 0, thumb=True)
        assert mode_zero == 3, mode_zero

        set_prop("adc1-ch4-sample", 0x3ff)
        convert_bank1()
        b1 = [mmio_read32(0x7f031014 + ch * 4) for ch in range(7)]
        assert b1 == [0, 0, 0, 0, 0x3ff, 0, 0], b1
        for _ in range(4):
            mode_idle = call(0x06547a0e, b1[4], 0, thumb=True)
        assert mode_idle == 0, mode_idle
        st = adc_state()
        assert st["adc1_ch4_sample"] == 0x3ff
        assert st["banks"][1]["results"] == b1
        report["checks"].append("original 0x06547a0e decodes ADC1 ch4=0 -> mode 3 and ADC1 ch4=0x3ff -> mode 0 (unplugged)")

    report["passed"] = True
    (args.output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": True, "checks": len(report["checks"]), "output": str(args.output)}, indent=2))


if __name__ == "__main__":
    main()
