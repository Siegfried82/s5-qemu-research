#!/usr/bin/env python3
"""Run a fresh synthetic frame through the real LUT and driver/software-JPU path.

This is a reproducible offline capture experiment, not the S5 shutter task.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Optional host image, cropped to 6000x4000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    output = (args.output or HERE / "results" /
              ("photo-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"passed": False, "scope": "fresh host frame -> actual BIN LUT in QEMU -> original driver -> software JPU -> guest JPEG",
              "camera_menu_or_shutter_executed": False, "jpeg_backend": "host Pillow software device"}
    commands = [[sys.executable, str(HERE / "capture.py"), "--output", str(output / "lut")],
                [sys.executable, str(HERE / "probe_jpu_model.py"), "--capture-dir", str(output / "lut"),
                 "--output", str(output / "jpu")]]
    if args.input:
        commands[0] += ["--input", str(args.input.resolve())]
    try:
        for command in commands:
            subprocess.run(command, check=True, timeout=120)
        lut = json.loads((output / "lut/verification.json").read_text())
        jpu = json.loads((output / "jpu/verification.json").read_text())
        if not lut["passed"] or not jpu["passed"]:
            raise RuntimeError("An underlying experiment did not pass")
        report.update(passed=True, export=jpu["export"], original_driver_status=jpu["after_software_completion"]["words"],
                      evidence=[str(output / "lut/verification.json"), str(output / "jpu/verification.json")])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    if not report["passed"]:
        print("离线图像实验失败，记录：", output / "result.json", file=sys.stderr)
        return 1
    print("离线图像实验通过（软件 JPU，未运行机身快门/菜单）：", output / "jpu/guest-output.jpg")
    return 0


if __name__ == "__main__":
    sys.exit(main())
