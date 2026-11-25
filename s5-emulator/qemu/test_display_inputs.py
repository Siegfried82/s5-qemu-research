#!/usr/bin/env python3
"""Synthetic-MMIO smoke for explicit display TEST inputs. No firmware loaded."""
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
    parser.add_argument("--output", type=Path, default=HERE / "display-test-smoke")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    binary = ROOT / "toolchain/build/qemu-system-arm"
    report = {"scope": "No firmware loaded; synthetic ARM load/store helpers test actual GIC access",
              "guest_instructions_executed": "Synthetic MMIO LDR/STR and BX only",
              "hardware_timing_verified": False,
              "qemu_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "checks": []}

    def check(name, result):
        report["checks"].append({"name": name, "passed": bool(result)})
        if not result:
            raise AssertionError(name)

    try:
        with QemuArm(binary, output / "qemu", machine="s5-rtos", smp=4, ram="512M",
                     address_ranges=[(0, 0x20000000), (0x70000000, 0x10000000),
                                     (0x80000000, 0x20000000)]) as q:
            q.select_cpu(0)
            # GIC register handlers require an executing CPU context. GDB
            # physical MMIO access can dereference absent current_cpu in QEMU.
            q.write_mem(0x10000, struct.pack("<4I", 0xe5901000, 0xe12fff1e,
                                          0xe5801000, 0xe12fff1e))
            q.breakpoint_add(0x10010)
            def gic_access(a, value=None):
                for register, v in [("cpsr", 0x1d3), ("sp", 0x24000),
                                    ("lr", 0x10010), ("r0", a),
                                    ("r1", value or 0),
                                    ("pc", 0x10000 if value is None else 0x10008)]:
                    q.write_reg(register, v)
                q.resume_cpu(0, timeout=2)
                if q.read_reg("pc") != 0x10010:
                    raise RuntimeError("Synthetic GIC helper did not return")
                return q.read_reg("r1")
            def get(prop):
                return q.qmp.command("qom-get", {"path": "/machine", "property": prop})
            def put(prop, value):
                return q.qmp.command("qom-set", {"path": "/machine", "property": prop, "value": value})
            def rejected(prop, value):
                try:
                    put(prop, value)
                except RuntimeError as e:
                    report.setdefault("expected_rejections", []).append(str(e))
                    return True
                return False
            def read32(a):
                if 0x70001000 <= a < 0x70002000:
                    return gic_access(a)
                return struct.unpack("<I", q.read_mem(a, 4))[0]
            def write32(a, v):
                if 0x70001000 <= a < 0x70002000:
                    gic_access(a, v)
                    return
                q.write_mem(a, struct.pack("<I", v))
            def values():
                return [read32(0x7c00011c), read32(0x7c050ef0)]
            check("default disabled with zero mask", get("display-test-mode") is False
                  and get("display-test-status-mask") == 0)
            check("default MMIO unchanged", values() == [0, 0])
            check("pulse rejected while disabled", rejected("display-test-pulse", 196))
            put("display-test-status-mask", 3)
            check("mask inactive while disabled", values() == [0, 0])
            put("display-test-mode", True)
            for mask, expected in [(0, [0, 0]), (1, [1, 0]), (2, [0, 1 << 28]), (3, [1, 1 << 28])]:
                put("display-test-status-mask", mask)
                check("independent mask " + str(mask), values() == expected)
            check("byte access uses correct little-endian lane",
                  q.read_mem(0x7c050ef3, 1) == b"\x10" and q.read_mem(0x7c050ef0, 2) == b"\0\0"
                  and q.read_mem(0x7c00011c, 1) == b"\x01")
            write32(0x7c00011c, 0xffffffff)
            write32(0x7c050ef0, 0xffffffff)
            check("guest writes do not create extra status bits", values() == [1, 1 << 28])
            check("other MMIO remains original zero", read32(0x7c000118) == 0 and read32(0x7c050ef4) == 0)
            check("invalid mask rejected", rejected("display-test-status-mask", 4)
                  and get("display-test-status-mask") == 3)
            for irq in (0, 195, 224, 231, 233, 448):
                check("invalid pulse " + str(irq), rejected("display-test-pulse", irq))
            # Configure actual GIC SPI inputs as edge triggered, then verify
            # the pending bit from each explicit pulse and clear it normally.
            for irq in (196, 230, 232):
                cfg = 0x70001c00 + 4 * (irq // 16)
                shift = (irq % 16) * 2
                write32(cfg, (read32(cfg) & ~(3 << shift)) | (2 << shift))
                target = 0x70001800 + (irq & ~3)
                target_shift = (irq % 4) * 8
                write32(target, (read32(target) & ~(0xff << target_shift)) | (1 << target_shift))
                word, bit = irq // 32, 1 << (irq % 32)
                write32(0x70001100 + word * 4, bit)
                write32(0x70001280 + word * 4, bit)
                check("IRQ starts clear " + str(irq), not read32(0x70001200 + word * 4) & bit)
                put("display-test-pulse", irq)
                check("pulse reaches actual GIC " + str(irq), bool(read32(0x70001200 + word * 4) & bit))
                write32(0x70001280 + word * 4, bit)
                check("ordinary GIC pending clear " + str(irq), not read32(0x70001200 + word * 4) & bit)
            put("display-test-mode", False)
            check("disable restores original read values", values() == [0, 0])
            check("disable rejects new pulse", rejected("display-test-pulse", 232))
            check("state property is read-only", rejected("display-test-state-json", "{}"))
            state = json.loads(get("display-test-state-json"))
            report["state"] = state
            check("accepted and rejected request accounting", state["pulse_requests"] == 11
                  and state["pulse_rejected"] == 8 and state["pulses"] == {"196": 1, "230": 1, "232": 1})
            check("read counters and explicit test-only scope", all(x > 0 for x in state["reads"].values())
                  and state["automatic_timing"] is False and state["hardware_clock_verified"] is False
                  and state["guest_callback_or_flag_writes"] is False and state["enabled"] is False)
        report["passed"] = True
    except Exception as exc:
        report["passed"] = False
        report["error"] = str(exc)
        raise
    finally:
        (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({"passed": report["passed"], "checks": len(report["checks"]),
                          "output": str(output)}, indent=2))


if __name__ == "__main__":
    main()
