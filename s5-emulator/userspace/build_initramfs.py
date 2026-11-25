#!/usr/bin/env python3
"""Build a no-libc ARM initramfs for executing stock S5 Linux userland commands.

Requires only Python and clang with the ARM assembler. No linker is required:
init.S uses one code/data section. This builder resolves only local ARM branch
and absolute-address relocations, rejecting unsupported/external relocations.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_COMMAND = 'echo S5_USERSPACE; /bin/cat /proc/version; /bin/ls /; echo "bash=$BASH_VERSION"'


def code_from_object(data):
    if data[:7] != b"\x7fELF\x01\x01\x01":
        raise ValueError("Compiler did not produce ELF32 little-endian")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    if header[1:3] != (1, 40):
        raise ValueError("Expected an ARM relocatable object")
    shoff, shsize, shnum, shstridx = header[6], header[11], header[12], header[13]
    if shsize != 40:
        raise ValueError("Unexpected ELF section-header size")
    sections = [struct.unpack_from("<10I", data, shoff+i*shsize) for i in range(shnum)]
    ss = sections[shstridx]
    names = data[ss[4]:ss[4]+ss[5]]
    text_index = None
    for i, section in enumerate(sections):
        begin = section[0]
        name = names[begin:names.index(0, begin)].decode("ascii")
        if name == ".text":
            text_index = i
    if text_index is None:
        raise ValueError("No .text section")
    for section in sections:
        if section[2] & 2 and section[1] in (1, 8) and section[5] and section != sections[text_index]:
            raise ValueError("Unexpected allocated data outside .text")
    text = sections[text_index]
    code = bytearray(data[text[4]:text[4]+text[5]])
    if not code or len(code) > 4096:
        raise ValueError("Unexpected /init code size")
    for section in sections:
        if section[1] not in (4, 9) or section[7] != text_index or not section[5]:
            continue
        if section[1] != 9 or section[9] != 8:
            raise ValueError("Only ELF REL local ARM branches are supported")
        symtab = sections[section[6]]
        for pos in range(section[4], section[4]+section[5], 8):
            place, info = struct.unpack_from("<II", data, pos)
            symbol = struct.unpack_from("<IIIBBH", data, symtab[4]+(info >> 8)*symtab[9])
            kind = info & 255
            if kind not in (2, 28, 29) or symbol[5] != text_index:
                raise ValueError("Unsupported or external relocation in /init")
            word = struct.unpack_from("<I", code, place)[0]
            if kind == 2:  # R_ARM_ABS32: .text will be loaded at 0x11000.
                struct.pack_into("<I", code, place, (word+symbol[1]+0x11000) & 0xffffffff)
                continue
            if word & 0x0e000000 != 0x0a000000:
                raise ValueError("ARM branch relocation did not point to a branch")
            addend = word & 0xffffff
            if addend & 0x800000:
                addend -= 0x1000000
            delta = symbol[1]+addend*4-place
            if delta % 4 or not -(1 << 25) <= delta < (1 << 25):
                raise ValueError("ARM branch is unaligned or out of range")
            struct.pack_into("<I", code, place, word & 0xff000000 | (delta >> 2) & 0xffffff)
    # _start must be at offset zero, checked from the object's symbol table.
    start_found = False
    for section in sections:
        if section[1] != 2:
            continue
        string_sec = sections[section[6]]
        strings = data[string_sec[4]:string_sec[4]+string_sec[5]]
        for pos in range(section[4], section[4]+section[5], section[9]):
            name, value, size, info, other, idx = struct.unpack_from("<IIIBBH", data, pos)
            text_name = strings[name:strings.index(0, name)]
            if text_name == b"_start":
                if value != 0 or idx != text_index:
                    raise ValueError("_start is not the first instruction")
                start_found = True
    if not start_found:
        raise ValueError("Object has no _start")
    return bytes(code)


def elf_executable(code):
    ident = b"\x7fELF\x01\x01\x01\x00" + bytes(8)
    size = 4096 + len(code)
    header = struct.pack("<16sHHIIIIIHHHHHH", ident, 2, 40, 1,
                         0x11000, 52, 0, 0x05000200, 52, 32, 1, 0, 0, 0)
    program = struct.pack("<8I", 1, 0, 0x10000, 0x10000, size, size, 5, 4096)
    return (header+program).ljust(4096, b"\x00")+code


def newc(entries):
    result = bytearray()
    for ino, (name, mode, data, major, minor) in enumerate(entries + [("TRAILER!!!", 0, b"", 0, 0)], 1):
        encoded = name.encode("utf-8")+b"\x00"
        fields = [ino, mode, 0, 0, 2 if stat.S_ISDIR(mode) else 1, 0, len(data),
                  0, 0, major, minor, len(encoded), 0]
        result += b"070701" + "".join("%08x" % value for value in fields).encode("ascii")
        result += encoded
        result += bytes((-len(result)) % 4)
        result += data
        result += bytes((-len(result)) % 4)
    result += bytes((-len(result)) % 512)
    return bytes(result)


def build(command, output, clang="clang", marker_token=None):
    raw_command = command.encode("utf-8")
    if not raw_command or b"\x00" in raw_command or len(raw_command) > 4095:
        raise ValueError("Command must be 1..4095 UTF-8 bytes with no NUL")
    if marker_token is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{16}", marker_token):
            raise ValueError("Marker token must be exactly 16 hexadecimal characters")
        marker_token = marker_token.lower()
    compiler = shutil.which(clang)
    if not compiler:
        raise ValueError("clang was not found; set --clang to an ARM-capable clang executable")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="s5-init-", dir=str(HERE)) as tmp:
        obj = Path(tmp)/"init.o"
        source = Path(tmp)/"init.S"
        assembly = (HERE/"init.S").read_text()
        if marker_token is not None:
            for marker in ("S5LAB_BEGIN", "S5LAB_ROOTFS_MOUNTED", "S5LAB_END",
                           "S5LAB_ERROR", "S5LAB_WARNING", "S5LAB_OUT"):
                assembly = assembly.replace(marker, marker+"_"+marker_token)
        source.write_text(assembly)
        subprocess.run([compiler, "--target=armv7a-none-eabi", "-march=armv7-a", "-marm",
                        "-c", str(source), "-o", str(obj)], check=True)
        code = code_from_object(obj.read_bytes())
    init = elf_executable(code)
    entries = [("dev", stat.S_IFDIR|0o755, b"", 0, 0),
               ("root", stat.S_IFDIR|0o755, b"", 0, 0),
               ("dev/kmsg", stat.S_IFCHR|0o600, b"", 1, 11),
               ("dev/null", stat.S_IFCHR|0o666, b"", 1, 3),
               ("dev/mtdblock0", stat.S_IFBLK|0o600, b"", 31, 0),
               ("dev/mtdblock1", stat.S_IFBLK|0o600, b"", 31, 1),
               ("command", stat.S_IFREG|0o400, raw_command, 0, 0),
               ("init", stat.S_IFREG|0o755, init, 0, 0)]
    archive = newc(entries)
    output.write_bytes(archive)
    init_path = output.with_suffix(".init.elf")
    init_path.write_bytes(init)
    os.chmod(init_path, 0o755)
    metadata = {"initramfs": str(output), "initramfs_bytes": len(archive),
                "initramfs_sha256": hashlib.sha256(archive).hexdigest(),
                "init_elf": str(init_path), "init_elf_sha256": hashlib.sha256(init).hexdigest(),
                "init_code_bytes": len(code), "command_bytes": len(raw_command),
                "execution": "original /bin/sh -c inside original SquashFS rootfs",
                "stdout": "pipe -> hexadecimal records in Linux /dev/kmsg", "marker_token": marker_token,
                "output_encoding": "hex", "output_chunk_max_bytes": 384,
                "output_marker": "S5LAB_OUT"+("_"+marker_token if marker_token else "")+" ",
                "end_marker": "S5LAB_END"+("_"+marker_token if marker_token else "")+" status=0x<raw wait4 status>",
                "camera_hardware_simulated": False}
    output.with_suffix(".json").write_text(json.dumps(metadata, indent=2)+"\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group()
    commands.add_argument("--command", default=None)
    commands.add_argument("--command-file", type=Path)
    parser.add_argument("--output", type=Path, default=HERE/"initramfs.cpio")
    parser.add_argument("--clang", default="clang")
    parser.add_argument("--marker-token", help="Optional 16 hexadecimal characters to distinguish this run's harness markers")
    args = parser.parse_args()
    try:
        command = args.command_file.read_text() if args.command_file else args.command
        result = build(DEFAULT_COMMAND if command is None else command, args.output, args.clang, args.marker_token)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, subprocess.CalledProcessError, struct.error) as exc:
        parser.exit(1, "Cannot build S5 initramfs: %s\n" % exc)


if __name__ == "__main__":
    main()
