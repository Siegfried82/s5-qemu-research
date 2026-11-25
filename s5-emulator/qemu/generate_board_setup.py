#!/usr/bin/env python3
"""Assemble board-setup.S and generate its byte array using Python stdlib only.

The blob is entered at its first byte, without a linker. Reject relocations,
allocated data outside .text, and an entry symbol that is not at offset zero.
"""
import argparse
import hashlib
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent


def code_from_object(data):
    if len(data) < 52 or data[:7] != b"\x7fELF\x01\x01\x01":
        raise ValueError("Expected an ELF32 little-endian object")
    header = struct.unpack_from("<16sHHIIIIIHHHHHH", data)
    if header[1:3] != (1, 40):
        raise ValueError("Expected an ARM relocatable object")
    shoff, shsize, shnum, shstridx = header[6], header[11], header[12], header[13]
    if shsize != 40 or not shnum or shstridx >= shnum:
        raise ValueError("Unsupported ELF section table")
    if shoff + shsize * shnum > len(data):
        raise ValueError("Truncated ELF section table")
    sections = [struct.unpack_from("<10I", data, shoff + i * shsize)
                for i in range(shnum)]

    def content(section):
        start, size = section[4:6]
        if start + size > len(data):
            raise ValueError("Truncated ELF section")
        return data[start:start + size]

    def name_at(strings, offset):
        if offset >= len(strings):
            raise ValueError("Invalid ELF string offset")
        return strings[offset:strings.index(0, offset)]

    names = content(sections[shstridx])
    text_indices = [i for i, section in enumerate(sections)
                    if name_at(names, section[0]) == b".text"]
    if len(text_indices) != 1:
        raise ValueError("Expected exactly one .text section")
    text_index = text_indices[0]
    for i, section in enumerate(sections):
        if section[1] in (4, 9) and section[5]:
            raise ValueError("Board setup must not contain relocations")
        if i != text_index and section[2] & 2 and section[5]:
            raise ValueError("Allocated data outside .text is unsupported")
    text = sections[text_index]
    if text[1] != 1 or not text[2] & 4:
        raise ValueError(".text is not executable PROGBITS")
    code = content(text)
    if not code or len(code) > 4096 or len(code) % 4:
        raise ValueError("Unexpected board setup size or ARM alignment")

    start_found = False
    for section in sections:
        if section[1] != 2:
            continue
        if section[9] != 16 or section[5] % 16 or section[6] >= shnum:
            raise ValueError("Invalid ELF symbol table")
        strings = content(sections[section[6]])
        symbols = content(section)
        for offset in range(0, len(symbols), 16):
            name, value, size, info, other, index = struct.unpack_from(
                "<IIIBBH", symbols, offset)
            if name_at(strings, name) == b"_start":
                if value != 0 or index != text_index:
                    raise ValueError("_start must be the first .text instruction")
                start_found = True
    if not start_found:
        raise ValueError("Object has no _start symbol")
    return code


def make_header(code):
    result = "/* Generated from board-setup.S; reconstructed research boot state. */\n"
    result += "static const uint8_t s5_board_setup[] = {\n"
    for offset in range(0, len(code), 12):
        result += "    " + ", ".join("0x%02x" % x for x in code[offset:offset + 12]) + ",\n"
    return result + "};\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify the existing header without writing")
    parser.add_argument("--clang", default="clang", help="ARM-capable clang executable")
    args = parser.parse_args()
    compiler = shutil.which(args.clang)
    if not compiler:
        parser.error("clang was not found; install Xcode Command Line Tools or set --clang")
    with tempfile.TemporaryDirectory(prefix="s5-board-setup-") as tmp:
        obj = Path(tmp) / "board-setup.o"
        subprocess.run([compiler, "--target=arm-none-eabi", "-march=armv7-a", "-marm",
                        "-c", str(HERE / "board-setup.S"), "-o", str(obj)], check=True)
        code = code_from_object(obj.read_bytes())
    header = make_header(code)
    output = HERE / "board-setup.h"
    current = output.read_text() if output.exists() else None
    if args.check and current != header:
        parser.error("board-setup.h is stale; run this script without --check")
    if not args.check and current != header:
        output.write_text(header)
    print("board-setup: %d bytes, no relocations, SHA-256 %s%s" % (
        len(code), hashlib.sha256(code).hexdigest(),
        "; header matches" if current == header else "; header updated"))


if __name__ == "__main__":
    main()
