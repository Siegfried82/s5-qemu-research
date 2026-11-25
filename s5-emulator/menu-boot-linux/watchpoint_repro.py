#!/usr/bin/env python3
"""Bounded synthetic ARM RAM stores testing QemuArm/GDB, not S5 firmware."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from probe.qemu_arm import QemuArm

CASES = {
    "control": None,
    "write-exact": "Z2,20000,4",
    "access-exact": "Z4,20000,4",
    "write-region": "Z2,20000,2000",
    "access-region": "Z4,20000,2000",
    "write-miss": "Z2,30000,4",
}


def worker(case, out):
    out.mkdir(parents=True, exist_ok=False)
    qemu = HERE.parent / "toolchain/build/qemu-system-arm"
    # ARM: str r1,[r0]; str r2,[r0,#4]; b .
    code = struct.pack("<3I", 0xe5801000, 0xe5802004, 0xeafffffe)
    (out / "synthetic-store.bin").write_bytes(code)
    report = {"scope": "Synthetic ARM RAM-only debugger test; no original S5 instructions",
              "case": case, "qemu_sha256": hashlib.sha256(qemu.read_bytes()).hexdigest(),
              "watch_request": CASES[case], "events": []}

    def record(stage, **fields):
        report["events"].append({"stage": stage, **fields})
        (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")

    start = time.monotonic()
    record("starting")
    try:
        with QemuArm(qemu, out, ram="1M", timeout=1) as emu:
            report["qemu_pid"] = emu.process.pid
            emu.write_mem(0x10000, code)
            for reg, value in {"r0": 0x20000, "r1": 0x11223344,
                               "r2": 0x55667788, "cpsr": 0x1d3,
                               "pc": 0x10000, "sp": 0x80000}.items():
                emu.write_reg(reg, value)
            emu.breakpoint_add(0x10008, kind=4)
            record("initialized", initial_pc=hex(emu.read_reg("pc")),
                   initial_memory=emu.read_mem(0x20000, 8).hex())
            if CASES[case]:
                record("watchpoint_inserted", reply=emu.rsp.request(CASES[case]))
            try:
                # Use the adapter's RSP directly so failed continue, QMP query,
                # and debugger interrupt are measured separately.
                reply = emu.rsp.request("c", timeout=1)
                record("continue_replied", reply=reply,
                       pc=hex(emu.read_reg("pc")),
                       memory=emu.read_mem(0x20000, 8).hex())
            except (OSError, RuntimeError, TimeoutError) as exc:
                record("continue_failed", exception=type(exc).__name__, error=str(exc))
            try:
                record("qmp_query", reply=emu.qmp.command("query-status", timeout=0.5))
            except (OSError, RuntimeError, TimeoutError) as exc:
                record("qmp_query_failed", exception=type(exc).__name__, error=str(exc))
            if not any(e["stage"] == "continue_replied" for e in report["events"]):
                try:
                    record("rsp_interrupt", reply=emu.rsp.interrupt(timeout=0.5))
                except (OSError, RuntimeError, TimeoutError) as exc:
                    record("rsp_interrupt_failed", exception=type(exc).__name__, error=str(exc))
            record("closing")
        record("closed", qemu_returncode=emu.process.returncode)
    except Exception as exc:
        record("worker_error", exception=type(exc).__name__, error=str(exc))
    report["wall_seconds"] = round(time.monotonic() - start, 3)
    (out / "result.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=tuple(CASES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.worker, args.output)
        return
    out = (args.output or HERE / "results" / ("watchpoint-repro-" + uuid.uuid4().hex[:8])).resolve()
    out.mkdir(parents=True, exist_ok=False)
    summary = {"scope": "Synthetic RAM store tests; no firmware or camera", "cases": []}
    for case in CASES:
        with (out / (case + ".stdout.log")).open("wb") as stdout, (out / (case + ".stderr.log")).open("wb") as stderr:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                "--worker", case, "--output", str(out / case)],
                stdout=stdout, stderr=stderr, start_new_session=True)
            timed_out = False
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=2)
        result_path = out / case / "result.json"
        result = json.loads(result_path.read_text()) if result_path.exists() else {}
        entry = {"case": case, "outer_timeout": timed_out,
                 "worker_returncode": process.returncode, "result": result}
        summary["cases"].append(entry)
        (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({"case": case, "outer_timeout": timed_out,
                          "events": result.get("events", [])}), flush=True)
    print("Evidence: " + str(out))


if __name__ == "__main__":
    main()
