#!/usr/bin/env python3
"""Bounded execution from S5's documented main-program handoff, not menu emulation.

Instruction breakpoints and execution bounds stop the run. Optional data
watchpoints are disabled by default after they made this QEMU unresponsive.
All RAM not loaded from the selected file starts zero. The low/high
copies are independent, not coherent physical aliases; MMU/SoC boot state is
not reconstructed. No target code is patched and no camera is connected.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from probe.qemu_arm import QemuArm
from qemu.generate_board_setup import code_from_object

PROGRAM_BASE = 0x06280000
WATCH_REGIONS = [
    (0x70000000, 0x10000000, "known high peripheral address region"),
    (0x1c000000, 0x04000000, "candidate lower peripheral alias region"),
    (0x00060000, 0x2000, "documented boot IPC register region"),
]
VECTORS = {0x00: "reset", 0x04: "undefined instruction", 0x08: "SVC",
           0x0c: "prefetch abort", 0x10: "data abort", 0x18: "IRQ", 0x1c: "FIQ"}


def system_registers(emu):
    """Read selected CP15 values through QEMU's own register description."""
    xml = emu.rsp.read_feature("system-registers.xml")
    (emu.output_dir / "system-registers.xml").write_text(xml)
    values = {}
    next_reg = None
    for reg in ET.fromstring(xml).iter("reg"):
        if "regnum" in reg.attrib:
            next_reg = int(reg.attrib["regnum"], 0)
        if next_reg is None:
            continue
        name = reg.attrib["name"]
        if any(part in name.upper() for part in ("SCTLR", "CPACR", "NSACR", "TTBR", "TTBCR", "DACR", "VBAR", "DFAR", "DFSR", "IFAR", "IFSR", "MPIDR")):
            raw = bytes.fromhex(emu.rsp.request("p%x" % next_reg))
            values[name] = {"number": next_reg, "value": hex(int.from_bytes(raw, "little"))}
        next_reg += 1
    return values


def enable_vfp_state(emu):
    with tempfile.TemporaryDirectory(prefix="s5-vfp-state-") as tmp:
        obj = Path(tmp) / "enable-vfp.o"
        subprocess.run(["clang", "--target=arm-none-eabi", "-march=armv7-a", "-marm",
                        "-c", str(HERE / "enable-vfp.S"), "-o", str(obj)], check=True)
        helper = code_from_object(obj.read_bytes())
    (emu.output_dir / "enable-vfp-helper.bin").write_bytes(helper)
    emu.write_mem(0x10000, helper)
    emu.write_reg("cpsr", 0x600001d3)
    emu.write_reg("pc", 0x10000)
    emu.write_reg("lr", 0x10100)
    emu.breakpoint_add(0x10100, kind=4)
    reply = emu.resume(timeout=1)
    if emu.read_reg("pc") != 0x10100:
        raise RuntimeError("VFP state helper did not return")
    emu.breakpoint_remove(0x10100, kind=4)
    return {"description": "Synthetic loader state: CPACR CP10/11 access and FPEXC.EN",
            "helper_address": "0x10000", "helper_sha256": hashlib.sha256(helper).hexdigest(),
            "stop_reply": reply, "system_registers_after_helper": system_registers(emu)}


def enable_mmu_state(emu):
    # 1 MiB section mappings: identity addresses, Normal non-cacheable RAM.
    # 0x70000000..0x7fffffff remains Strongly-ordered candidate peripheral space.
    # This reconstructs an explicitly synthetic mapping, not a recovered table.
    descriptors = [(index << 20) | (0xc02 if 0x700 <= index < 0x800 else 0x11c02)
                   for index in range(4096)]
    table = struct.pack("<4096I", *descriptors)
    emu.write_mem(0x100000, table)
    (emu.output_dir / "synthetic-page-table.bin").write_bytes(table)
    with tempfile.TemporaryDirectory(prefix="s5-mmu-state-") as tmp:
        obj = Path(tmp) / "enable-mmu.o"
        subprocess.run(["clang", "--target=arm-none-eabi", "-march=armv7-a", "-marm",
                        "-c", str(HERE / "enable-mmu.S"), "-o", str(obj)], check=True)
        helper = code_from_object(obj.read_bytes())
    (emu.output_dir / "enable-mmu-helper.bin").write_bytes(helper)
    emu.write_mem(0x10200, helper)
    emu.write_reg("cpsr", 0x600001d3)
    emu.write_reg("pc", 0x10200)
    emu.write_reg("lr", 0x10300)
    emu.breakpoint_add(0x10300, kind=4)
    reply = emu.resume(timeout=1)
    if emu.read_reg("pc") != 0x10300:
        raise RuntimeError("MMU state helper did not return")
    emu.breakpoint_remove(0x10300, kind=4)
    return {"description": "Synthetic identity short-section page table; Normal non-cacheable RAM",
            "peripheral_range_strongly_ordered": "0x70000000..0x7fffffff",
            "table_address": "0x100000", "table_sha256": hashlib.sha256(table).hexdigest(),
            "helper_sha256": hashlib.sha256(helper).hexdigest(), "stop_reply": reply,
            "system_registers_after_helper": system_registers(emu)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("original", "dump"), default="original")
    parser.add_argument("--seconds", type=float, default=3)
    parser.add_argument("--output", type=Path, default=HERE / "results-original")
    parser.add_argument("--entry", type=lambda v: int(v, 0), default=PROGRAM_BASE + 4)
    parser.add_argument("--enable-vfp", action="store_true", help="explicitly reconstruct loader VFP state")
    parser.add_argument("--enable-mmu", action="store_true", help="install an explicitly synthetic Normal-RAM identity mapping")
    watches = parser.add_mutually_exclusive_group()
    watches.add_argument("--watchpoints", dest="watchpoints", action="store_true",
                         help="enable experimental data watchpoints (can hang this QEMU)")
    watches.add_argument("--no-watchpoints", dest="watchpoints", action="store_false",
                         help="use instruction breakpoints only (default)")
    parser.set_defaults(watchpoints=False)
    parser.add_argument("--release-peer-flags", action="store_true",
                        help="synthetically clear two peer-core completion flags at the observed barrier")
    parser.add_argument("--serial-status", type=lambda v: int(v, 0),
                        help="synthetic 16-bit status for 0x7f04300c at its observed polling instruction")
    parser.add_argument("--checkpoint", type=lambda v: int(v, 0), help="additional original-code milestone breakpoint")
    parser.add_argument("--steps-after-checkpoint", type=int, default=0,
                        help="bounded single steps after the milestone (max 1000)")
    args = parser.parse_args()
    if not 0 < args.seconds <= 20:
        parser.error("--seconds must be >0 and <=20")
    if not 0 <= args.steps_after_checkpoint <= 1000 or (args.steps_after_checkpoint and args.checkpoint is None):
        parser.error("--steps-after-checkpoint requires --checkpoint and a count <=1000")
    if args.serial_status is not None and not 0 <= args.serial_status <= 0xffff:
        parser.error("--serial-status must fit uint16")
    project = HERE.parent.parent
    source = (project / "s5-v29-offline/unpacked-v29/03_program.bin" if args.source == "original"
              else Path.home() / "相机功能验证/data/program_0x86280000.bin")
    data = source.read_bytes()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    report = {"scope": "main-program early startup only", "source": str(source),
              "source_kind": args.source, "source_sha256": hashlib.sha256(data).hexdigest(),
              "source_bytes": len(data), "entry": hex(args.entry), "entry_state": "ARM",
              "original_instructions_patched": False, "menu_booted": False,
              "camera_connected": False, "timeout_seconds": args.seconds,
              "assumptions": ["single Cortex-A7 core with MPIDR core index zero",
                  "QEMU reset has flat zero RAM and MMU off; explicit synthetic setup is recorded when requested",
                  "source copied independently at 0x06280000 and 0x86280000; no coherent alias",
                  "documented warm-handoff CPSR/SP/r0-r3/LR, remaining registers zero",
                  "no SoC/MMIO, other-core, interrupt-controller or display implementation"]}
    qemu = HERE.parent / "toolchain/build/qemu-system-arm"
    with QemuArm(qemu, out, loads=[(source, PROGRAM_BASE), (source, 0x86280000)]) as emu:
        if args.enable_vfp:
            report["loader_state_reconstruction"] = enable_vfp_state(emu)
            report["assumptions"].append("bootloader is assumed to have enabled CP10/11 and FPEXC.EN")
        if args.enable_mmu:
            report["synthetic_mmu_state"] = enable_mmu_state(emu)
            report["assumptions"].append("synthetic 1 MiB identity section table at 0x100000; normal uncached RAM; MMU on")
        for i in range(16):
            emu.write_reg(i, 0)
        # Historical handoff probe: these are initial-state assumptions here,
        # not a claim that QEMU reproduces the complete bootloader state.
        for reg, value in {"cpsr": 0x600001d3, "sp": 0x13000, "lr": 0x3c4,
                           "r0": 0x800, "r1": 0x00c5187d, "r2": 3,
                           "r3": 0xffffffff, "pc": args.entry}.items():
            emu.write_reg(reg, value)
        report["initial_registers"] = {k: hex(v) for k, v in emu.registers().items()}
        report["initial_system_registers"] = system_registers(emu)
        report["watchpoints"] = []
        for address, size, label in (WATCH_REGIONS if args.watchpoints else []):
            reply = emu.rsp.request("Z4,%x,%x" % (address, size))
            if reply != "OK":
                raise RuntimeError("Cannot set bounded hardware-access watchpoint: " + reply)
            report["watchpoints"].append({"address": hex(address), "size": hex(size), "label": label})
        for address in VECTORS:
            emu.breakpoint_add(address, kind=4)
        if args.checkpoint is not None:
            emu.breakpoint_add(args.checkpoint, kind=2)
            report["checkpoint"] = hex(args.checkpoint)
        if args.release_peer_flags:
            emu.breakpoint_add(0x063b0124, kind=2)
        if args.serial_status is not None:
            emu.breakpoint_add(0x0642c458, kind=2)
        (out / "initial-state.json").write_text(json.dumps(report, indent=2) + "\n")
        start = time.monotonic()
        responsive = True
        try:
            stop = emu.resume(timeout=args.seconds)
            if args.release_peer_flags and emu.read_reg("pc") == 0x063b0124:
                if emu.read_mem(0x063b0124, 8) != bytes.fromhex("002101201a6054f8"):
                    raise RuntimeError("Original startup barrier instructions changed")
                before = emu.read_mem(0x209c0, 16)
                emu.write_mem(0x209c4, bytes(8))
                report["synthetic_peer_completion"] = {
                    "pc": "0x063b0124", "shared_address": "0x209c0",
                    "before_words": [hex(x) for x in struct.unpack("<4I", before)],
                    "after_words": [hex(x) for x in struct.unpack("<4I", emu.read_mem(0x209c0, 16))],
                    "limitation": "only flags cleared; no peer CPU instructions or initialization performed"}
                emu.breakpoint_remove(0x063b0124, kind=2)
                stop = emu.resume(timeout=args.seconds)
            if args.serial_status is not None and emu.read_reg("pc") == 0x0642c458:
                if emu.read_reg("r6") != 0x7f043000 or emu.read_mem(0x0642c458, 4) != bytes.fromhex("b6f80cc0"):
                    raise RuntimeError("Serial status poll does not match the observed instruction")
                before = emu.read_mem(0x7f04300c, 2)
                emu.write_mem(0x7f04300c, args.serial_status.to_bytes(2, "little"))
                report["synthetic_serial_status"] = {
                    "pc": "0x0642c458", "address": "0x7f04300c",
                    "before": hex(int.from_bytes(before, "little")), "after": hex(args.serial_status),
                    "limitation": "static status bits only; no device response data, FIFO or interrupts modeled"}
                emu.breakpoint_remove(0x0642c458, kind=2)
                stop = emu.resume(timeout=args.seconds)
            report["stop_reply"] = stop
            watch = re.search(r"(?:a?watch|rwatch):([0-9a-fA-F]+)", stop)
            if watch:
                report["stop_reason"] = "before first candidate hardware memory access"
                report["access_address"] = "0x" + watch.group(1)
            else:
                report["stop_reason"] = "breakpoint or other GDB stop"
            if args.steps_after_checkpoint and emu.read_reg("pc") == args.checkpoint:
                emu.breakpoint_remove(args.checkpoint, kind=2)
                report["single_step_trace"] = []
                for index in range(args.steps_after_checkpoint):
                    regs = emu.registers()
                    event = {"index": index, "pc": hex(regs["r15"]),
                             "cpsr": hex(regs["cpsr"]),
                             "instruction_hex": emu.read_mem(regs["r15"], 4).hex(),
                             "registers": {k: hex(v) for k, v in regs.items()}}
                    report["single_step_trace"].append(event)
                    (out / "step-trace.json").write_text(json.dumps(report["single_step_trace"], indent=2) + "\n")
                    event["stop_reply"] = emu.step(timeout=1)
                    if "watch:" in event["stop_reply"] or emu.read_reg("pc") in VECTORS:
                        report["stop_reason"] = "watchpoint or exception during bounded single stepping"
                        break
                else:
                    report["stop_reason"] = "single-step instruction bound reached"
        except (TimeoutError, socket.timeout) as exc:
            report["stop_reason"] = "execution time bound reached; interrupted"
            if isinstance(exc, socket.timeout):
                responsive = False
                report["stop_reason"] = "QEMU became unresponsive; process will be terminated"
                report["communication_error"] = str(exc)
        report["wall_seconds"] = round(time.monotonic() - start, 6)
        registers = emu.registers() if responsive else {}
        report["final_registers"] = {k: hex(v) for k, v in registers.items()}
        report["final_system_registers"] = system_registers(emu) if responsive else {}
        if responsive:
            report["startup_shared_words"] = [hex(x) for x in struct.unpack("<4I", emu.read_mem(0x209c0, 16))]
            report["serial_register_bytes_7f043000"] = emu.read_mem(0x7f043000, 32).hex()
        pc = registers.get("r15", 0)
        report["final_instruction_state"] = ("Thumb" if registers["cpsr"] & 0x20 else "ARM") if responsive else "unknown"
        if responsive and pc in VECTORS:
            report["exception_vector"] = VECTORS[pc]
        try:
            if responsive:
                report["pc_window_address"] = hex(max(0, pc - 16))
                report["pc_window_hex"] = emu.read_mem(max(0, pc - 16), 64).hex()
        except (RuntimeError, ValueError) as exc:
            report["pc_window_error"] = str(exc)
        report["hmp_registers"] = emu.qmp.command("human-monitor-command", {"command-line": "info registers"}) if responsive else "QEMU unresponsive"
        (out / "registers.txt").write_text(report["hmp_registers"])
    report["qemu_exit_code"] = emu.process.returncode
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report.get(key) for key in ("source_kind", "entry", "stop_reason",
        "access_address", "exception_vector", "final_instruction_state", "final_registers",
        "wall_seconds", "qemu_exit_code", "menu_booted")}, indent=2))


if __name__ == "__main__":
    main()
