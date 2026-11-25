#!/usr/bin/env python3
"""Execute synthetic ARM/Thumb arithmetic and exercise probe cleanup."""
import argparse
import hashlib
import json
from pathlib import Path
import time

from qemu_arm import QemuArm

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, default=HERE.parent / "toolchain/build/qemu-system-arm")
    parser.add_argument("--output", type=Path, default=HERE / "results/smoke")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Synthetic arithmetic only; no S5 firmware executed", "cases": []}
    for thumb in (False, True):
        name, kind = ("thumb", 2) if thumb else ("arm", 4)
        # r0 = r0 + r1; r0 += 7; return through LR.
        code = bytes.fromhex("401807307047" if thumb else "010080e0070080e21eff2fe1")
        address, stop = 0x10000, 0x20000
        source = args.output / (name + ".bin")
        source.write_bytes(code)
        with QemuArm(args.qemu, args.output / name, loads=[(source, address)]) as emu:
            assert emu.read_mem(address, len(code)) == code
            emu.write_reg("cpsr", 0x1d3 | (0x20 if thumb else 0))
            emu.write_reg("r0", 12)
            emu.write_reg("r1", 23)
            emu.write_reg("sp", 0x30000)
            emu.write_reg("lr", stop | int(thumb))
            emu.write_reg("pc", address)
            emu.write_mem(stop, bytes.fromhex("fee7" if thumb else "feffffea"))
            emu.breakpoint_add(stop, kind=kind)
            first_stop = emu.step(timeout=2)
            assert emu.read_reg("r0") == 35
            assert emu.read_reg("pc") == address + kind
            final_stop = emu.resume(timeout=2)
            assert emu.read_reg("r0") == 42
            assert emu.read_reg("pc") == stop
            assert bool(emu.read_reg("cpsr") & 0x20) == thumb
            emu.breakpoint_remove(stop, kind=kind)
            dump = emu.dump_memory(address, len(code), args.output / name / "code-dump.bin")
            assert dump.read_bytes() == code
            report["cases"].append({"name": name, "input_r0": 12, "input_r1": 23,
                "after_one_step": 35, "returned_r0": 42, "return_pc": hex(stop),
                "step_reply": first_stop, "return_reply": final_stop,
                "registers": {k: hex(v) for k, v in emu.registers().items()},
                "code_sha256": hashlib.sha256(code).hexdigest(), "pass": True})
        assert emu.process.poll() == 0

    with QemuArm(args.qemu, args.output / "timeout") as emu:
        emu.write_mem(0x10000, bytes.fromhex("feffffea"))
        emu.write_reg("cpsr", 0x1d3)
        emu.write_reg("pc", 0x10000)
        start = time.monotonic()
        try:
            emu.resume(timeout=0.1)
        except TimeoutError:
            elapsed = time.monotonic() - start
            assert emu.read_reg("pc") == 0x10000
            report["timeout_interrupt"] = {"pass": True, "elapsed_seconds": round(elapsed, 3)}
        else:
            raise AssertionError("Infinite loop did not time out")
    assert emu.process.poll() == 0
    report["all_passed"] = True
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
