#!/usr/bin/env python3
"""Bounded original-code SSP trace; only instruction breakpoints, no Z2/Z4."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time

HERE = Path(__file__).resolve().parent
EMULATOR = HERE.parent
sys.path.insert(0, str(EMULATOR))
from probe.qemu_arm import QemuArm
from register_model import ColdRtcRegisters, UnknownRegister

spec = importlib.util.spec_from_file_location("stock_boot_probe", EMULATOR / "menu-boot-rtos/probe_boot.py")
boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boot)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", choices=("capture", "cold-rtc"), default="capture")
    parser.add_argument("--max-transfers", type=int, default=32)
    parser.add_argument("--initial-register", action="append", default=[], metavar="ADDRESS=BYTE",
                        help="explicit experimental reset field; never presented as recovered hardware state")
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    source = EMULATOR.parent / "s5-v29-offline/unpacked-v29/03_program.bin"
    data = source.read_bytes()
    report = {"source": str(source), "source_sha256": hashlib.sha256(data).hexdigest(),
              "original_instructions_patched": False, "watchpoints": [],
              "assumptions": ["single core, synthetic VFP/MMU initial state", "two peer flags cleared without executing peer cores", "flat RAM, independent low/high program copies"],
              "events": []}
    components = [("03_program.bin", 0x06280000), ("05_postboot1.bin", 0x07267000),
                  ("06_postboot2.bin", 0x076e7000), ("07_postboot3.bin", 0x07707000),
                  ("08_postboot4.bin", 0x07ee7000), ("09_postboot5.bin", 0x07f07000),
                  ("21_osddata.bin", 0x07fe7000)]
    loads = [(source.parent / name, address) for name, address in components]
    loads += [(source, 0x86280000)]
    report["component_set"] = [{"name": path.name, "address": hex(address),
                                "bytes": path.stat().st_size,
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                               for path, address in loads]
    with QemuArm(EMULATOR / "toolchain/build/qemu-system-arm", out, timeout=2,
                 loads=loads) as emu:
        report["qemu_pid"] = emu.process.pid
        boot.enable_vfp_state(emu)
        boot.enable_mmu_state(emu)
        for reg in range(16):
            emu.write_reg(reg, 0)
        for reg, value in {"cpsr": 0x600001d3, "sp": 0x13000, "lr": 0x3c4,
                           "r0": 0x800, "r1": 0x00c5187d, "r2": 3,
                           "r3": 0xffffffff, "pc": 0x06280004}.items():
            emu.write_reg(reg, value)
        emu.breakpoint_add(0x063b0124, 2)
        emu.resume(timeout=3)
        assert emu.read_reg("pc") == 0x063b0124
        report["peer_flags_before"] = emu.read_mem(0x209c0, 16).hex()
        emu.write_mem(0x209c4, bytes(8))
        emu.breakpoint_remove(0x063b0124, 2)
        emu.breakpoint_add(0x0642c43c, 2)
        emu.resume(timeout=3)
        regs = emu.registers()
        assert regs["r15"] == 0x0642c43c
        report["entry_registers"] = {name: hex(value) for name, value in regs.items()}
        report["tx_buffer"] = emu.read_mem(regs["r1"], min(regs["r3"], 1024)).hex()
        report["rx_before"] = emu.read_mem(regs["r2"], min(regs["r3"], 1024)).hex() if regs["r2"] else None
        report["stack_address"] = hex(regs["r13"])
        report["stack_words"] = [hex(v) for v in struct.unpack("<64I", emu.read_mem(regs["r13"], 256))]
        report["controller_table"] = emu.read_mem(0x08ab472c, 28).hex()
        report["registers_7f043000"] = emu.read_mem(0x7f043000, 32).hex()
        if args.profile == "cold-rtc":
            model = ColdRtcRegisters()
            overrides = {}
            for item in args.initial_register:
                address, value = [int(value, 0) for value in item.split("=", 1)]
                if address & 1 or not 0 <= address < 65536 or not 0 <= value < 256:
                    raise ValueError("Initial register requires even uint16 address and uint8 value")
                model.values[address] = value
                model.sources[address] = "explicit experimental command-line reset assumption"
                overrides[hex(address)] = value
            report["model"] = model.description()
            report["model"]["explicit_overrides"] = overrides
            report["transfers"] = []
            for address in (4, 8, 12, 16, 24, 28):
                emu.breakpoint_add(address, 4)
            emu.breakpoint_add(0x06495ab6, 2)
            emu.breakpoint_add(0x064232f8, 2)
            deadline = time.monotonic() + 20
            for transfer_index in range(args.max_transfers):
                regs = emu.registers()
                if regs["r15"] not in (0x0642c43c, 0x064232f8):
                    report["stop_reason"] = "outside serial transfer"
                    break
                if regs["r0"] != 3 or regs["r3"] != 3:
                    report["stop_reason"] = "unmodeled serial channel or frame length"
                    break
                tx = emu.read_mem(regs["r1"], regs["r3"])
                rx_address = regs["r2"]
                return_pc = regs["r14"] & ~1
                entry_pc = regs["r15"]
                event = {"index": transfer_index, "tx": tx.hex(),
                         "entry_pc": hex(entry_pc),
                         "cs_register": hex(int.from_bytes(emu.read_mem(0x7f081c0c, 4), "little")),
                         "entry_stack_words": [hex(v) for v in struct.unpack("<32I", emu.read_mem(regs["r13"], 128))],
                         "io": []}
                report["transfers"].append(event)
                model.begin()
                queue = []
                emu.breakpoint_remove(entry_pc, 2)
                for step_index in range(1000):
                    if time.monotonic() > deadline:
                        raise TimeoutError("Serial model global 20 second bound reached")
                    pc = emu.read_reg("pc")
                    if pc == return_pc:
                        break
                    if not (0x0642c43c <= pc <= 0x0642c51a or 0x064232f8 <= pc <= 0x064233aa):
                        raise RuntimeError("Unexpected instruction outside serial transfer: %x" % pc)
                    # PL022-compatible status, derived from our FIFO state.
                    # Each completed SPI byte produces one receive byte.
                    status = 3 | (4 if queue else 0) | (8 if len(queue) >= 8 else 0)
                    emu.write_mem(0x7f04300c, struct.pack("<H", status))
                    if pc in (0x0642c478, 0x0642c4c6, 0x06423350):
                        value = emu.read_reg("r5" if pc == 0x0642c478 else "r12") & 0xff
                        event["io"].append({"pc": hex(pc), "tx": value, "status": status})
                        try:
                            response = model.transfer(value)
                        except UnknownRegister as exc:
                            event["unsupported"] = str(exc)
                            break
                        emu.step(timeout=1)
                        queue.append(response)
                    elif pc in (0x0642c486, 0x0642c490, 0x0642c49c, 0x0642c4d8, 0x0642c4f6,
                                0x06423326, 0x06423364, 0x06423384):
                        if not queue:
                            raise RuntimeError("Stock driver read an empty RX FIFO")
                        value = queue.pop(0)
                        emu.write_mem(0x7f043008, struct.pack("<H", value))
                        event["io"].append({"pc": hex(pc), "rx": value, "status": status})
                        emu.step(timeout=1)
                    else:
                        emu.step(timeout=1)
                else:
                    raise RuntimeError("Serial transfer exceeded 1000 instruction limit")
                if "unsupported" in event:
                    report["stop_reason"] = event["unsupported"]
                    break
                event["rx"] = emu.read_mem(rx_address, 3).hex()
                event["instruction_steps"] = step_index
                event["register_event"] = model.events[-1]
                (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
                emu.breakpoint_add(entry_pc, 2)
                try:
                    emu.resume(timeout=2)
                except TimeoutError:
                    report["stop_reason"] = "no next serial transfer within 2 seconds"
                    break
            else:
                report["stop_reason"] = "transfer bound reached"
            report["final_registers"] = {name: hex(value) for name, value in emu.registers().items()}
            report["model_events"] = model.events
            report["menu_booted"] = False
    report["qemu_exit_code"] = emu.process.returncode
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
