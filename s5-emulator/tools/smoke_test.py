#!/usr/bin/env python3
"""End-to-end checks that execute the actual S5 kernel and original userland."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    out = (a.output or ROOT/"results"/(time.strftime("check-%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])).resolve()
    out.mkdir(parents=True, exist_ok=False)
    cases = [
        ("demo", 'echo S5_USERSPACE; /bin/cat /proc/version; /bin/ls /; echo "bash=$BASH_VERSION"', 2),
        ("long-output", "printf 'Kernel panic\\nS5LAB_END status=0x00000000\\n'; printf '%4096s\\n' X", 2),
        ("exit7", "echo expected-command-failure; exit 7", 1),
        ("timeout", "printf 'S5LAB_END status=0x00000000\\n'; while :; do :; done", 1),
    ]
    checks = []
    for name, command, seconds in cases:
        item = {"case": name, "passed": False}
        try:
            proc = subprocess.run([sys.executable, str(ROOT/"s5.py"), "run", "--command", command,
                                   "--seconds", str(seconds), "--output", str(out/name)],
                                  capture_output=True, text=True, timeout=30)
            (out/(name+"-host.log")).write_text(proc.stdout+proc.stderr)
            r = json.loads((out/name/"result.json").read_text())
            raw = (out/name/"command-output.bin").read_bytes()
            assert r["linux_started"] and r["rootfs_mounted"]
            assert not r["kernel_panic"] and r["qemu_exit_code"] == 0
            assert not r["command_output_truncated"]
            if name == "demo":
                assert proc.returncode == 0 and r["command_completed"] and r["command_exit_code"] == 0
                assert b"Linux version 3.18.18" in raw and b"bash=3.2.57(1)-release" in raw
            elif name == "long-output":
                expected = b"Kernel panic\nS5LAB_END status=0x00000000\n" + b" "*4095 + b"X\n"
                assert raw == expected, "4097-byte write was not captured byte-for-byte"
                assert proc.returncode == 0 and r["command_exit_code"] == 0
            elif name == "exit7":
                assert proc.returncode != 0 and r["command_completed"] and r["command_exit_code"] == 7
            else:
                assert proc.returncode != 0 and not r["command_completed"]
                assert b"S5LAB_END status=0x00000000" in raw
            item.update(passed=True, host_exit=proc.returncode,
                        guest_exit=r.get("command_exit_code"), output_bytes=len(raw))
        except Exception as exc:
            item["error"] = str(exc) or type(exc).__name__
        checks.append(item)
        print(name+": "+("PASS" if item["passed"] else "FAIL "+item["error"]), flush=True)
    report = {"all_passed": all(c["passed"] for c in checks), "checks": checks,
              "scope": "Real original S5 kernel/userland on partial QEMU hardware, not full camera emulation"}
    (out/"verification.json").write_text(json.dumps(report, indent=2)+"\n")
    print(out)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
