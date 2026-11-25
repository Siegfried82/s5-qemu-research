#!/usr/bin/env python3
"""Run the original S5 V2.9 Linux in a partial, offline QEMU machine."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
import uuid

HERE = Path(__file__).resolve().parent
FIRMWARE = HERE.parent / "s5-v29-offline"
PARTS = FIRMWARE / "unpacked-v29"
SOURCE_SHA256 = "b2ec4aeec209693b874df8dfdf11fc85b38fa4675b44ec554b7a167f430827e1"
sys.path.insert(0, str(HERE / "analysis"))
from read_kmsg import read_kmsg


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def verify():
    fw = (FIRMWARE / "S5___V29.bin").read_bytes()
    if sha(fw) != SOURCE_SHA256:
        raise ValueError("Original S5 V2.9 firmware SHA-256 mismatch")
    if zlib.crc32(fw[0x200:]) & 0xffffffff != struct.unpack_from("<I", fw, 0x40)[0]:
        raise ValueError("Original update CRC mismatch")
    checked = []
    for index in range(48):
        pos = 0x2ec + index * 0x5c
        name = fw[pos:pos+12].rstrip(b"\0").decode("ascii")
        data = (PARTS / ("%02d_%s.bin" % (index, name))).read_bytes()
        size = struct.unpack_from("<I", fw, pos+16)[0]
        expected = fw[pos+0x1c:pos+0x3c].hex()
        if size != len(data) or sha(data) != expected:
            raise ValueError("Component verification failed: " + name)
        checked.append({"name": name, "bytes": size, "sha256": expected})
    if len(checked) != 48:
        raise ValueError("Expected all 48 component records")
    return {"source_sha256": SOURCE_SHA256, "verified_components": 48,
            "components": checked}


def fdt_parse(data):
    h = struct.unpack_from(">10I", data)
    if h[0] != 0xd00dfeed or h[1] > len(data):
        raise ValueError("Invalid FDT")
    strings = data[h[3]:h[3]+h[8]]
    pos = h[2]
    stack = []
    root = None
    while pos < h[2]+h[9]:
        token = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if token == 1:
            end = data.index(b"\0", pos)
            node = {"name": data[pos:end].decode(), "props": {}, "children": []}
            pos = (end+4) & ~3
            if stack:
                stack[-1]["children"].append(node)
            else:
                root = node
            stack.append(node)
        elif token == 2:
            stack.pop()
        elif token == 3:
            size, nameoff = struct.unpack_from(">II", data, pos)
            pos += 8
            name = strings[nameoff:strings.index(b"\0", nameoff)].decode()
            stack[-1]["props"][name] = data[pos:pos+size]
            pos = (pos+size+3) & ~3
        elif token == 4:
            continue
        elif token == 9:
            break
        else:
            raise ValueError("Unknown FDT token")
    return root


def fdt_build(root):
    strings = bytearray()
    offsets = {}
    blob = bytearray()

    def u32(*v):
        blob.extend(struct.pack(">"+"I"*len(v), *v))

    def padded(b):
        blob.extend(b)
        blob.extend(b"\0" * (-len(b) % 4))

    def visit(node):
        u32(1)
        padded(node["name"].encode()+b"\0")
        for key, val in node["props"].items():
            if key not in offsets:
                offsets[key] = len(strings)
                strings.extend(key.encode()+b"\0")
            u32(3, len(val), offsets[key])
            padded(val)
        for child in node["children"]:
            visit(child)
        u32(2)
    visit(root)
    u32(9)
    # The inspected factory DTB has no reservations. Linux memory range is preserved.
    offstruct = 56
    offstrings = offstruct+len(blob)
    header = struct.pack(">10I", 0xd00dfeed, offstrings+len(strings), offstruct,
                         offstrings, 40, 17, 16, 0, len(strings), len(blob))
    return header + bytes(16) + blob + strings


def prepare():
    verified = verify()
    root = fdt_parse((PARTS / "28_dtb.bin").read_bytes())
    for node in root["children"]:
        if node["name"] == "cpus":
            cpu = node["children"][0]
            cpu["name"] = "cpu@0"
            cpu["props"]["reg"] = struct.pack(">I", 0)
        if node["name"].startswith("interrupt-controller@"):
            node["props"].pop("cpu-offset", None)
        if node["name"].startswith("sdio@"):
            node["props"]["status"] = b"disabled\0"
        if node["name"] == "chosen":
            original = node["props"]["bootargs"].rstrip(b"\0")
            node["props"]["bootargs"] = original + b" log_buf_len=262144\0"
    out = HERE / "prepared"
    out.mkdir(exist_ok=True)
    dtb = fdt_build(root)
    (out / "s5-linux.dtb").write_bytes(dtb)
    verified.update({"experimental_dtb_sha256": sha(dtb),
                     "dtb_changes": ["Linux CPU physical ID 3 -> 0 (single emulated CPU)",
                                     "Removed nonbanked GIC cpu-offset",
                                     "Disabled unmodeled SDIO nodes",
                                     "Expanded printk buffer to 256 KiB for captured command output"],
                     "bootloader_reconstruction": "GIC distributor enabled; SPI routing and priorities initialized for CPU0",
                     "hardware_stubs": ["clock registers 0x7f038000: RAM-backed",
                                        "IPC registers 0x00060000: RAM-backed",
                                        "Other unmodeled MMIO: read zero, drop write, log first 256"]})
    write_json(out / "manifest.json", verified)
    return out


class QMP:
    def __init__(self, path, process, timeout=20):
        self.sock = socket.socket(socket.AF_UNIX)
        deadline = time.monotonic()+timeout
        while True:
            try:
                self.sock.connect(str(path))
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if process.poll() is not None:
                    raise RuntimeError("QEMU exited before opening QMP (see qemu-stderr.log)")
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for QEMU QMP")
                time.sleep(0.05)
        self.sock.settimeout(20)
        self.file = self.sock.makefile("rwb", buffering=0)
        greeting = json.loads(self.file.readline())
        if "QMP" not in greeting:
            raise RuntimeError("Invalid QMP greeting")
        self.command("qmp_capabilities")

    def command(self, name, arguments=None):
        msg = {"execute": name}
        if arguments is not None:
            msg["arguments"] = arguments
        self.file.write(json.dumps(msg).encode()+b"\n")
        while True:
            line = self.file.readline()
            if not line:
                raise RuntimeError("QEMU closed QMP")
            result = json.loads(line)
            if "error" in result:
                raise RuntimeError(str(result["error"]))
            if "return" in result:
                return result["return"]

    def hmp(self, command):
        return self.command("human-monitor-command", {"command-line": command})

    def close(self):
        self.file.close()
        self.sock.close()


def nearest_symbol(regs):
    match = re.search(r"R15=([0-9a-fA-F]+)", regs)
    if not match:
        return None
    pc = int(match.group(1), 16)
    if 0xffff0000 <= pc < 0xffff1000:
        return {"pc": hex(pc), "symbol": "ARM high exception vectors", "offset": hex(pc-0xffff0000)}
    if not 0xc0008000 <= pc < 0xc04c028c:
        return {"pc": hex(pc), "symbol": None, "offset": None}
    symbols = HERE / "analysis/System.map"
    best = None
    if symbols.exists():
        for line in symbols.read_text().splitlines():
            addr, kind, name = line.split(maxsplit=2)
            address = int(addr, 16)
            if kind in "Tt" and address <= pc and (best is None or address >= best[0]):
                best = (address, name)
    return {"pc": hex(pc), "symbol": best[1] if best else None,
            "offset": hex(pc-best[0]) if best else None}


def boot(args):
    prep = prepare()
    qemu = args.qemu.resolve()
    if not qemu.exists():
        raise FileNotFoundError("QEMU not built: " + str(qemu))
    output = args.output or HERE / "results" / (time.strftime(args.action+"-%Y%m%d-%H%M%S")+"-"+uuid.uuid4().hex[:6])
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    initrd = args.initrd
    guest_command = getattr(args, "command", None)
    marker_token = uuid.uuid4().hex[:16] if guest_command is not None else None
    if guest_command is not None:
        initrd = output / "initramfs.cpio"
        built = subprocess.run([sys.executable, str(HERE / "userspace/build_initramfs.py"),
                                "--command", guest_command, "--output", str(initrd),
                                "--marker-token", marker_token],
                               capture_output=True, text=True)
        (output / "initramfs-build.log").write_text(built.stdout+built.stderr)
        if built.returncode:
            raise ValueError("Cannot build command initramfs: " + built.stderr.strip())
    command = [str(qemu), "-M", "s5-linux", "-accel", "tcg", "-m", "64M",
               "-display", "none", "-nodefaults", "-serial", "file:"+str(output / "serial.log"),
               "-kernel", str(PARTS / "29_zimage.bin"), "-dtb", str(prep / "s5-linux.dtb"),
               "-device", "loader,file="+str(PARTS / "30_rootfs1.bin")+",addr=0x83100000,force-raw=on",
               "-device", "loader,file="+str(PARTS / "31_rootfs2.bin")+",addr=0x83400000,force-raw=on",
               "-d", "unimp,guest_errors", "-D", str(output / "mmio.log")]
    if initrd:
        command += ["-initrd", str(initrd.resolve())]
    if args.append:
        command += ["-append", args.append]
    if args.gdb:
        command += ["-gdb", "tcp:127.0.0.1:"+str(args.gdb)]
    write_json(output / "invocation.json", {"argv": command, "run_seconds": args.seconds,
                                            "qemu_sha256": sha(qemu.read_bytes()),
                                            "guest_command": guest_command,
                                            "marker_token": marker_token,
                                            "derived_dtb_sha256": sha((prep / "s5-linux.dtb").read_bytes())})
    result = {"kind": "S5 Linux partial-system emulation", "original_kernel_modified": False,
              "full_camera_boot_verified": False, "output": str(output), "kernel_log_records": 0}
    process = None
    qmp = None
    try:
        # Short temporary pathname avoids macOS Unix-domain socket length limits.
        with tempfile.TemporaryDirectory(prefix="s5q-", dir="/tmp") as tmp, \
                (output / "qemu-stderr.log").open("wb") as stderr:
            command += ["-qmp", "unix:"+tmp+"/qmp,server=on,wait=off"]
            process = subprocess.Popen(command, stdout=stderr, stderr=stderr, stdin=subprocess.DEVNULL)
            qmp = QMP(Path(tmp) / "qmp", process)
            deadline = time.monotonic()+args.seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("QEMU stopped unexpectedly: " + str(process.returncode))
                time.sleep(0.1)
            qmp.command("stop")
            regs = qmp.hmp("info registers")
            (output / "registers.txt").write_text(regs)
            result["stopped_at"] = nearest_symbol(regs)
            ramfile = output / "ram.bin"
            answer = qmp.hmp("pmemsave 0x80000000 0x4000000 " + json.dumps(str(ramfile), ensure_ascii=False))
            if answer.strip():
                raise RuntimeError("RAM dump failed: " + answer)
            snapshot = ramfile.read_bytes()
            try:
                logs = read_kmsg(snapshot, base=0x80000000)
                write_json(output / "kernel-log.json", logs)
                text = "".join("[%10.6f] %s\n" % (r["timestamp_ns"]/1e9, r["text"]) for r in logs["records"])
                (output / "kernel.log").write_text(text)
                result["kernel_log_records"] = len(logs["records"])
                result["linux_started"] = any("Linux version" in r["text"] and r["facility"] == 0 for r in logs["records"])
                root_marker = "S5LAB_ROOTFS_MOUNTED" + ("_"+marker_token if marker_token else "")
                result["rootfs_mounted"] = (any("VFS: Mounted root" in r["text"] and r["facility"] == 0 for r in logs["records"])
                                             or any(r["text"].strip() == root_marker for r in logs["records"]))
                result["kernel_panic"] = any("Kernel panic" in r["text"] and r["facility"] == 0 for r in logs["records"])
                if guest_command is not None:
                    begin_seen = any(r["text"].strip() == "S5LAB_BEGIN_"+marker_token for r in logs["records"])
                    end_pattern = r"S5LAB_END_"+marker_token+r" status=0x[0-9a-fA-F]{8}"
                    end_records = [r for r in logs["records"] if re.fullmatch(end_pattern, r["text"].strip())]
                    result["command_completed"] = bool(end_records)
                    result["command_output_truncated"] = not begin_seen and logs["first_sequence"] > 0
                    if end_records:
                        status = int(end_records[-1]["text"].split("0x")[1], 16)
                        result["command_wait_status"] = status
                        result["command_exit_code"] = os.WEXITSTATUS(status) if os.WIFEXITED(status) else None
                        result["command_signal"] = os.WTERMSIG(status) if os.WIFSIGNALED(status) else None
                    else:
                        result["command_error"] = "Guest command did not finish within --seconds, or output/boot failed"
                    capture = bytearray()
                    out_prefix = "S5LAB_OUT_"+marker_token+" "
                    for r in logs["records"]:
                        line = r["text"]
                        if line.startswith(out_prefix):
                            capture.extend(bytes.fromhex(line[len(out_prefix):]))
                    transcript = capture.decode("utf-8", "replace")
                    result["command_output_bytes"] = len(capture)
                    (output / "command-output.bin").write_bytes(capture)
                    (output / "command-output.txt").write_text(transcript)
                    print(transcript, end="")
                else:
                    print(text, end="")
            except ValueError as exc:
                result["kernel_log_error"] = str(exc)
            if not args.keep_ram:
                ramfile.unlink()
            qmp.command("quit")
            process.wait(timeout=5)
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        if qmp:
            try:
                qmp.close()
            except OSError as exc:
                result["qmp_close_error"] = str(exc)
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if process:
            result["qemu_exit_code"] = process.returncode
        write_json(output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    failed = (result.get("error") or result.get("kernel_log_error") or result.get("qemu_exit_code") != 0
              or not result.get("linux_started") or result.get("kernel_panic"))
    if guest_command is not None:
        failed = (failed or not result.get("command_completed") or result.get("command_exit_code") != 0
                  or result.get("command_output_truncated"))
    return 1 if failed else 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "video":
        return subprocess.call([sys.executable, str(HERE.parent / "s5-video-unlocker/video.py"),
                                *sys.argv[2:]])
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("video", help="S5 native 6K/5.9K menu tools (separate from QEMU)")
    sub.add_parser("verify", help="Verify all components against the original update table")
    sub.add_parser("prepare", help="Verify inputs and create an explicitly modified emulator DTB")
    for name, help_text in [("boot", "Run original kernel and init; save printk and MMIO logs"),
                            ("run", "Run a command using original S5 Bash and original Linux kernel")]:
        b = sub.add_parser(name, help=help_text)
        b.add_argument("--qemu", type=Path, default=HERE / "toolchain/build/qemu-system-arm")
        b.add_argument("--seconds", type=float, default=3)
        b.add_argument("--output", type=Path)
        b.add_argument("--keep-ram", action="store_true")
        b.add_argument("--initrd", type=Path)
        b.add_argument("--append")
        b.add_argument("--gdb", type=int, help="Open debugger port on loopback only")
        if name == "run":
            b.add_argument("--command", default='echo S5_USERSPACE; /bin/cat /proc/version; /bin/ls /; echo "bash=$BASH_VERSION"')
    args = parser.parse_args()
    try:
        if args.action == "verify":
            result = verify()
            print("Verified %d / 48 original S5 V2.9 components." % result["verified_components"])
        elif args.action == "prepare":
            print(prepare())
        else:
            if not 0.2 <= args.seconds <= 60:
                parser.error("--seconds must be between 0.2 and 60")
            return boot(args)
    except (OSError, ValueError, RuntimeError) as exc:
        print("S5 emulator: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
