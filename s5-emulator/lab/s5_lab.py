#!/usr/bin/env python3
"""Run bounded, real S5 V2.9 firmware fragments in Unicorn; never access a camera."""
import argparse
import hashlib
import importlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import time

WORKSPACE = Path(__file__).resolve().parents[2]
DUMP_BASE = 0x86280000
MODULE_BASE = 0x87707000
GETTER = 0x87864ACC
MEMCPY = 0x8782BD3C
CALL = 0x877537A2
CAVE = 0x879F7900
HOOK_ENTRY = 0x879F7D10
SENTINEL = 0x8D000000
DESCRIPTOR = 0x8E000000
STACK = 0x8F000000
SOURCE_DESCRIPTOR = 0x0B3D4224
Y_ADDRESS, C_ADDRESS = 0x90000000, 0x92000000
STRIDE, HEIGHT, WIDTH, TILE = 6016, 4000, 6000, 120
DUMP_SHA = "7ae165083396d4c9213f7a1c019ac56823d0ed4196fa14b8eb25e553a45a21e4"
PATCH_SHA = "f6fea9e6a030e77c34424ee65abb17ec7d7c179b3ebd83f6627eed6dc324758b"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def thumb_bl(source, target):
    value = (target - source - 4) & 0x1FFFFFF
    sign, i1, i2 = (value >> 24) & 1, (value >> 23) & 1, (value >> 22) & 1
    return struct.pack("<HH", 0xF000 | sign << 10 | ((value >> 12) & 0x3FF),
                       0xD000 | (1 ^ sign ^ i1) << 13 | (1 ^ sign ^ i2) << 11 |
                       ((value >> 1) & 0x7FF))


def load_assets(args):
    dump_path = args.dump.expanduser().resolve()
    patch_path = args.assets.expanduser().resolve() / "postboot3-lut-experiment.bin"
    payload_path = args.assets.expanduser().resolve() / "payload.bin"
    dump, patch, payload = dump_path.read_bytes(), patch_path.read_bytes(), payload_path.read_bytes()
    checks = {
        "official_v29_dump_sha256": sha(dump) == DUMP_SHA,
        "known_experimental_component_sha256": sha(patch) == PATCH_SHA,
        "payload_matches_component": bool(payload) and
            patch[CAVE - MODULE_BASE:CAVE - MODULE_BASE + len(payload)] == payload,
        "original_call_targets_getter":
            dump[CALL - DUMP_BASE:CALL - DUMP_BASE + 4] == thumb_bl(CALL, GETTER),
        "patched_call_targets_hook":
            patch[CALL - MODULE_BASE:CALL - MODULE_BASE + 4] == thumb_bl(CALL, HOOK_ENTRY),
    }
    identity = {
        "dump": {"path": str(dump_path), "bytes": len(dump), "sha256": sha(dump)},
        "patched_component": {"path": str(patch_path), "bytes": len(patch), "sha256": sha(patch)},
        "payload": {"path": str(payload_path), "bytes": len(payload), "sha256": sha(payload)},
        "checks": checks,
        "verified": all(checks.values()),
    }
    return dump, patch, payload, identity


def load_unicorn(args):
    if args.dependency_path:
        sys.path.insert(0, str(args.dependency_path.expanduser().resolve()))
    return importlib.import_module("unicorn"), importlib.import_module("unicorn.arm_const")


def run_case(args, case, assets, unicorn, arm):
    dump, patch, payload, _ = assets
    uc = unicorn.Uc(unicorn.UC_ARCH_ARM, unicorn.UC_MODE_ARM)
    uc.mem_map(0x86000000, 0x02000000)
    uc.mem_write(DUMP_BASE, dump)
    if case != "getter":
        uc.mem_write(CAVE, payload)
        uc.mem_write(CALL, patch[CALL - MODULE_BASE:CALL - MODULE_BASE + 4])
    for address, size in [(0x0B3D0000, 0x10000), (SENTINEL, 0x1000),
                          (DESCRIPTOR, 0x20000), (STACK, 0x10000),
                          (Y_ADDRESS, 0x1800000), (C_ADDRESS, 0x1800000)]:
        uc.mem_map(address, size)
    plane_bytes = STRIDE * HEIGHT
    width = WIDTH - 1 if case == "guard" else WIDTH
    descriptor = bytearray(0x38)
    struct.pack_into("<IIHHIIHHHHI", descriptor, 0, Y_ADDRESS, plane_bytes, STRIDE, HEIGHT,
                     C_ADDRESS, plane_bytes, STRIDE, HEIGHT, 0, 0, 0)
    struct.pack_into("<HHI", descriptor, 0x1C, width, HEIGHT, 2)
    uc.mem_write(SOURCE_DESCRIPTOR, bytes(descriptor))
    # Enough initialized rows for requested tiles plus the following untouched tile.
    rows = min(HEIGHT, (args.tiles + WIDTH // TILE) // (WIDTH // TILE) + 1)
    y_initial = bytes([args.y]) * (STRIDE * rows)
    c_initial = bytes([args.cb, args.cr]) * (STRIDE * rows // 2)
    uc.mem_write(Y_ADDRESS, y_initial)
    uc.mem_write(C_ADDRESS, c_initial)
    uc.reg_write(arm.UC_ARM_REG_SP, STACK + 0xF000)
    uc.reg_write(arm.UC_ARM_REG_R0, 1)
    uc.reg_write(arm.UC_ARM_REG_R1, DESCRIPTOR)
    uc.reg_write(arm.UC_ARM_REG_LR, SENTINEL | 1)
    saved_registers = [getattr(arm, "UC_ARM_REG_R" + str(n)) for n in range(4, 12)]
    for register in saved_registers:
        uc.reg_write(register, 0x12345678)
    trace, visits = [], {"getter": 0, "memcpy": 0, "hook": 0, "lut": 0}
    stop = {"reason": None}
    endpoint = SENTINEL if case == "getter" else CALL + 4

    def event(name):
        def callback(machine, address, size, user):
            visits[name] += 1
            trace.append({"event": name, "pc": hex(address), "visit": visits[name]})
            if name == "lut" and visits[name] == args.tiles + 1:
                stop["reason"] = "requested_tiles_completed_before_next_tile"
                machine.emu_stop()
        return callback

    for address, name in [(GETTER, "getter"), (MEMCPY, "memcpy")]:
        uc.hook_add(unicorn.UC_HOOK_CODE, event(name), begin=address, end=address)
    if case != "getter":
        for address, name in [(HOOK_ENTRY, "hook"), (CAVE, "lut")]:
            uc.hook_add(unicorn.UC_HOOK_CODE, event(name), begin=address, end=address)

    def returned(machine, address, size, user):
        trace.append({"event": "returned", "pc": hex(address)})
        stop["reason"] = "returned_to_caller"
        machine.emu_stop()

    uc.hook_add(unicorn.UC_HOOK_CODE, returned, begin=endpoint, end=endpoint)
    fault = []

    def invalid_memory(machine, access, address, size, value, user):
        fault.append({"access": access, "address": hex(address), "bytes": size})
        return False

    uc.hook_add(unicorn.UC_HOOK_MEM_INVALID, invalid_memory)
    error = None
    started = time.monotonic()
    try:
        uc.emu_start((GETTER if case == "getter" else CALL) | 1, 0,
                     timeout=int(args.timeout * 1000000), count=args.instruction_limit)
    except unicorn.UcError as exc:
        error = str(exc)
        stop["reason"] = "emulation_error"
    if stop["reason"] is None:
        timed_out = bool(uc.query(unicorn.UC_QUERY_TIMEOUT))
        stop["reason"] = "time_limit" if timed_out else "instruction_limit_or_unexpected_stop"
    checks = {
        "original_getter_executed": visits["getter"] == 1,
        "original_memcpy_executed": visits["memcpy"] >= 1,
        "descriptor_copied_exactly": bytes(uc.mem_read(DESCRIPTOR, 0x38)) == bytes(descriptor),
        "no_memory_fault": not fault,
        "no_emulator_error": error is None,
    }
    if case != "getter":
        checks["experimental_hook_executed"] = visits["hook"] == 1
    tile_samples = []
    if case in ("getter", "guard"):
        checks.update({
            "returned_normally": stop["reason"] == "returned_to_caller",
            "return_status_zero": uc.reg_read(arm.UC_ARM_REG_R0) == 0,
            "stack_preserved": uc.reg_read(arm.UC_ARM_REG_SP) == STACK + 0xF000,
            "callee_saved_registers_preserved": all(
                uc.reg_read(register) == 0x12345678 for register in saved_registers),
            "image_unchanged": bytes(uc.mem_read(Y_ADDRESS, len(y_initial))) == y_initial and
                bytes(uc.mem_read(C_ADDRESS, len(c_initial))) == c_initial,
            "lut_not_called": visits["lut"] == 0,
        })
    else:
        checks["requested_tiles_completed"] = (
            stop["reason"] == "requested_tiles_completed_before_next_tile")
        expected_y = bytearray(y_initial)
        expected_c = bytearray(c_initial)
        completed = min(args.tiles, max(0, visits["lut"] - 1))
        first_output = None
        uniform = True
        for index in range(completed):
            row, column_tile = divmod(index, WIDTH // TILE)
            offset = row * STRIDE + column_tile * TILE
            ys = bytes(uc.mem_read(Y_ADDRESS + offset, TILE))
            cs = bytes(uc.mem_read(C_ADDRESS + offset, TILE))
            sample = [ys[0], cs[0], cs[1]]
            if first_output is None:
                first_output = sample
            uniform = uniform and ys == bytes([sample[0]]) * TILE and cs == bytes(sample[1:]) * (TILE // 2)
            uniform = uniform and sample == first_output
            expected_y[offset:offset + TILE] = bytes([sample[0]]) * TILE
            expected_c[offset:offset + TILE] = bytes(sample[1:]) * (TILE // 2)
            tile_samples.append({"tile": index, "row": row, "x": column_tile * TILE,
                                 "output_y_cb_cr": sample})
        checks["processed_tiles_uniform_and_consistent"] = completed == args.tiles and uniform
        checks["untouched_pixels_and_row_padding_preserved"] = (
            bytes(uc.mem_read(Y_ADDRESS, len(y_initial))) == expected_y and
            bytes(uc.mem_read(C_ADDRESS, len(c_initial))) == expected_c)
        if (args.y, args.cb, args.cr) == (100, 128, 128):
            checks["known_baseline_output_matches"] = first_output == [108, 125, 131]
    return {
        "case": case, "passed": all(checks.values()), "checks": checks,
        "stop_reason": stop["reason"], "elapsed_seconds": round(time.monotonic() - started, 6),
        "final_pc": hex(uc.reg_read(arm.UC_ARM_REG_PC)),
        "limits": {"timeout_seconds": args.timeout, "instructions": args.instruction_limit},
        "input": {"width": width, "height": HEIGHT, "stride": STRIDE,
                  "y_cb_cr": [args.y, args.cb, args.cr],
                  "requested_tiles": args.tiles if case == "tile" else 0},
        "tile_samples": tile_samples, "trace": trace, "memory_faults": fault, "error": error,
        "scope": "real firmware instructions; synthetic RAM and image; no hardware or full boot",
        "full_image_return_tested": False,
    }


def isolated_case(args, case):
    command = [sys.executable, str(Path(__file__).resolve()), "run", "--worker",
               "--case", case, "--dump", str(args.dump), "--assets", str(args.assets),
               "--tiles", str(args.tiles), "--y", str(args.y), "--cb", str(args.cb),
               "--cr", str(args.cr), "--timeout", str(args.timeout),
               "--instruction-limit", str(args.instruction_limit)]
    if args.dependency_path:
        command.extend(["--dependency-path", str(args.dependency_path)])
    try:
        process = subprocess.run(command, capture_output=True, text=True,
                                 timeout=args.timeout + 5)
    except subprocess.TimeoutExpired:
        return {"case": case, "passed": False, "stop_reason": "worker_wall_time_limit",
                "error": "Native worker exceeded the emulator limit plus 5 seconds setup allowance."}
    try:
        output = json.loads(process.stdout)
        if "results" in output:
            result = output["results"][0]
            if process.returncode != 0 and result.get("passed"):
                result.update({"passed": False, "stop_reason": "worker_failed_after_result",
                               "worker_exit_code": process.returncode})
            return result
        detail = output.get("error", "Worker produced no execution result.")
    except json.JSONDecodeError:
        detail = "Native worker exited without a JSON result."
    result = {"case": case, "passed": False, "stop_reason": "worker_failed",
              "worker_exit_code": process.returncode, "error": detail,
              "stderr": process.stderr[-2000:]}
    if process.returncode < 0:
        result["signal"] = -process.returncode
        result["help"] = "On macOS, restricted JIT execution can terminate Unicorn; use an allowed local terminal."
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("command", choices=["inspect", "run"])
    result.add_argument("--dump", type=Path,
                        default=Path.home() / "相机功能验证/data/program_0x86280000.bin")
    result.add_argument("--assets", type=Path,
                        default=WORKSPACE / "s5-v29-offline/lut-firmware-experiment")
    result.add_argument("--dependency-path", type=Path,
                        help="Optional installed Python package directory; normally use a venv.")
    result.add_argument("--case", choices=["all", "getter", "guard", "tile"], default="all")
    result.add_argument("--tiles", type=int, default=1, help="Completed 120-pixel tiles, 1..1000.")
    result.add_argument("--y", type=int, default=100)
    result.add_argument("--cb", type=int, default=128)
    result.add_argument("--cr", type=int, default=128)
    result.add_argument("--timeout", type=float, default=10, help="Seconds per case, 0.001..60.")
    result.add_argument("--instruction-limit", type=int, default=50000000)
    result.add_argument("--output", type=Path, help="Also save the JSON report to this file.")
    result.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return result


def main():
    ap = parser()
    args = ap.parse_args()
    if not 1 <= args.tiles <= 1000:
        ap.error("--tiles must be 1..1000")
    if not all(0 <= value <= 255 for value in (args.y, args.cb, args.cr)):
        ap.error("--y, --cb, --cr must be 0..255")
    if not 0.001 <= args.timeout <= 60 or not 1 <= args.instruction_limit <= 2000000000:
        ap.error("--timeout must be 0.001..60 and --instruction-limit 1..2000000000")
    report = {"tool": "s5-firmware-lab", "schema_version": 1, "command": args.command}
    code = 1
    try:
        assets = load_assets(args)
        report["assets"] = assets[3]
        if not assets[3]["verified"]:
            raise ValueError("Asset identity checks failed; firmware execution refused.")
        if args.command == "inspect":
            report["known_entrypoints"] = {
                "getter": hex(GETTER), "memcpy": hex(MEMCPY), "patched_call": hex(CALL),
                "experimental_hook": hex(HOOK_ENTRY), "lut_function": hex(CAVE)}
            report["passed"] = True
        else:
            unicorn, arm = load_unicorn(args)
            report["engine"] = {"name": "Unicorn", "version": unicorn.__version__,
                                "module": str(Path(unicorn.__file__).resolve())}
            cases = ["getter", "guard", "tile"] if args.case == "all" else [args.case]
            report["results"] = [run_case(args, case, assets, unicorn, arm) if args.worker
                                 else isolated_case(args, case) for case in cases]
            report["passed"] = all(case["passed"] for case in report["results"])
        code = 0 if report["passed"] else 1
    except (OSError, ValueError, ImportError) as exc:
        report.update({"passed": False, "error": str(exc)})
        if isinstance(exc, ImportError):
            report["dependency_help"] = "Install lab/requirements.txt in your venv, or pass --dependency-path."
    output = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    print(output, end="")
    return code


if __name__ == "__main__":
    sys.exit(main())
