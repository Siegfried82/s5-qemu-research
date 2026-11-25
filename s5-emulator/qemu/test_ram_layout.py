#!/usr/bin/env python3
"""Verify 2-GiB experimental backing and default 512-MiB aliases, no firmware."""
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=HERE / "results-ram-layout")
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "toolchain/build/qemu-system-arm"
    report = {"scope": "Synthetic ARM memory accesses only; no camera firmware",
              "physical_camera_capacity_verified": False,
              "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "cases": []}
    try:
        for ram, size in [("512M", 0x20000000), ("2G", 0x80000000)]:
            low_size = min(size, 0x40000000)
            case = {"ram": ram, "checks": []}
            report["cases"].append(case)
            def check(name, okay):
                case["checks"].append({"name": name, "passed": bool(okay)})
                if not okay:
                    raise AssertionError(ram + ": " + name)
            with QemuArm(binary, output / ram, machine="s5-rtos", smp=4, ram=ram,
                         address_ranges=[(0, low_size), (0x70000000, 0x10000000),
                                         (0x80000000, size)]) as q:
                q.select_cpu(0)
                q.write_mem(0x10000, struct.pack("<4I", 0xe5901000, 0xe12fff1e,
                                              0xe5801000, 0xe12fff1e))
                q.breakpoint_add(0x10010)
                def access(address, value=None):
                    for reg, number in [("cpsr", 0x1d3), ("sp", 0x24000),
                                        ("lr", 0x10010), ("r0", address),
                                        ("r1", value or 0),
                                        ("pc", 0x10000 if value is None else 0x10008)]:
                        q.write_reg(reg, number)
                    q.resume_cpu(0, timeout=2)
                    check("synthetic helper returned at " + hex(address), q.read_reg("pc") == 0x10010)
                    return q.read_reg("r1")
                def unmapped(address):
                    try:
                        q.rsp.request("m%x,4" % address)
                    except RuntimeError as exc:
                        if str(exc) == "GDB RSP error: E14":
                            return True
                        raise
                    return False
                layout = json.loads(q.qmp.command("qom-get", {"path": "/machine", "property": "ram-layout-json"}))
                case["layout"] = layout
                check("reported explicit layout", layout["backing_bytes"] == size
                      and layout["low_alias"] == {"base": 0, "size": low_size}
                      and layout["high_alias"] == {"base": 0x80000000, "size": size}
                      and layout["physical_camera_capacity_verified"] is False)
                access(0x20000, 0x12345678)
                check("low write visible at high alias", access(0x80020000) == 0x12345678)
                access(0x80020000, 0xaabbccdd)
                check("high write visible at low alias", access(0x20000) == 0xaabbccdd)
                end = low_size - 4
                access(end, 0x1122eeff)
                check("last low word shares high alias", access(end + 0x80000000) == 0x1122eeff)
                check("Device-range address is not DRAM", unmapped(0x40000000))
                # These remain the original controller/input behavior even
                # when the high address space uses the complete 2-GiB backing.
                access(0x7c160000, 0x10)
                check("module MMIO answers request rather than RAM echo", access(0x7c160000) == 0x11)
                check("GPIO sampled input remains MMIO", access(0x7f080410) == 0x10)
                check("UART empty FIFO remains MMIO", access(0x7f050018) == 0x90)
                access(0x70028000, 0x5a5aa5a5)
                check("SoC SRAM remains writable", access(0x70028000) == 0x5a5aa5a5)
                if ram == "2G":
                    addresses = [0x80030000, 0xa0030000, 0xc0030000, 0xe0030000,
                                 0xfd159000, 0xffa91000, 0xfffffffc,
                                 0xfc160000, 0xff050018, 0xf0028000]
                    expected = {}
                    for i, address in enumerate(addresses):
                        expected[address] = 0x40010000 + i
                        access(address, expected[address])
                    for address, value in expected.items():
                        check("distinct high backing " + hex(address), access(address) == value)
                    check("fd159000 does not wrap to low1d159000", access(0x1d159000) == 0)
                    check("ffa91000 does not wrap to low1fa91000", access(0x1fa91000) == 0)
                    check("highfc16 memory did not alias module MMIO", access(0x7c160000) == 0x11)
                    check("highff05 memory did not alias UART MMIO", access(0x7f050018) == 0x90)
                    check("highf002 memory did not alias SoC SRAM", access(0x70028000) == 0x5a5aa5a5)
                    case["high_patterns"] = {hex(k): hex(v) for k, v in expected.items()}
                else:
                    check("default high RAM still ends at9fffffff", unmapped(0xa0000000))
                case["passed"] = True
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
        raise
    finally:
        (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": report["passed"],
                          "checks": sum(len(x["checks"]) for x in report["cases"]),
                          "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
