#!/usr/bin/env python3
"""Run the original loader2 timer initializer in isolation on s5-rtos."""
import hashlib
import json
from pathlib import Path
import struct
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from probe.qemu_arm import QemuArm


def main():
    out = HERE / "results-loader-timer"
    out.mkdir(parents=True, exist_ok=False)
    source = HERE.parents[1] / "s5-v29-offline/unpacked-v29/01_loader2.bin"
    report = {"scope": "Original loader timer initializer, isolated from full boot",
              "source": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "entry_file_offset": "0x5e6", "loader_loaded_at": "0x0",
              "original_instructions_patched": False,
              "synthetic_component": "return breakpoint and parked-core ARM loops at 0x200000",
              "no_full_loader_or_main_firmware_boot_claimed": True}
    with QemuArm(HERE.parent / "toolchain/build/qemu-system-arm", out / "qemu",
                 machine="s5-rtos", smp=4, ram="512M", loads=[(source, 0)],
                 address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000)]) as emu:
        emu.write_mem(0x200000, struct.pack("<2I", 0xeafffffe, 0xeafffffe))
        for index in range(4):
            emu.select_cpu(index)
            emu.write_reg("cpsr", 0x1d3)
            emu.write_reg("pc", 0x200004)
        emu.select_cpu(0)
        emu.write_reg("cpsr", 0x1f3)
        emu.write_reg("pc", 0x5e6)
        emu.write_reg("lr", 0x200000)
        report["before_control"] = hex(int.from_bytes(emu.read_mem(0x7f020028, 4), "little"))
        emu.breakpoint_add(0x200000, 4)
        report["stop_reply"] = emu.resume_all(timeout=1)
        assert emu.read_reg("pc") == 0x200000
        report["after_control"] = hex(int.from_bytes(emu.read_mem(0x7f020028, 4), "little"))
        report["first_value"] = int.from_bytes(emu.read_mem(0x7f020024, 4), "little")
        emu.breakpoint_remove(0x200000, 4)
        try:
            emu.resume_all(timeout=0.03)
        except TimeoutError:
            pass
        report["second_value"] = int.from_bytes(emu.read_mem(0x7f020024, 4), "little")
        report["elapsed_ticks_mod32"] = (report["first_value"] - report["second_value"]) & 0xffffffff
        report["passed"] = report["after_control"] == "0x86" and 0 < report["elapsed_ticks_mod32"] < 10000000
    report["qemu_exit_code"] = emu.process.returncode
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
