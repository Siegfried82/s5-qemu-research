#!/usr/bin/env python3
"""Extract and inspect the verified stock S5 V2.9 ARM Linux image, offline."""
import argparse
import hashlib
import json
from pathlib import Path
import struct
import zlib

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = "72b6633d7f20009c5067af6faa5549d52a581c4a55915695e616d666220f7ef0"
KERNEL_SHA = "84f99f8b9d4200a30fe2968e8a3fa29aea6d00aa90c153929e399208d1d3048a"


def inspect(source, output):
    data = source.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != EXPECTED:
        raise ValueError("Not the verified S5 V2.9 zImage; version-specific offsets must not be reused")
    offset = data.index(b"\x1f\x8b\x08")
    decoder = zlib.decompressobj(31)
    raw = decoder.decompress(data[offset:]) + decoder.flush()
    if not decoder.eof or hashlib.sha256(raw).hexdigest() != KERNEL_SHA:
        raise ValueError("Kernel decompression/hash verification failed")

    # These offsets are independently recovered from this exact kernel's
    # monotonic kallsyms address table, encoded names, markers and token table.
    count = struct.unpack_from("<I", raw, 0x385250)[0]
    if count != 28401:
        raise ValueError("Unexpected kallsyms symbol count")
    addresses = struct.unpack_from("<%dI" % count, raw, 0x369680)
    tokens = []
    pos = 0x3d4840
    token_offsets = []
    for _ in range(256):
        token_offsets.append(pos - 0x3d4840)
        end = raw.index(0, pos)
        tokens.append(raw[pos:end].decode("ascii"))
        pos = end + 1
    if tuple(token_offsets) != struct.unpack_from("<256H", raw, 0x3d4bd0):
        raise ValueError("kallsyms token-index mismatch")
    pos = 0x385260
    symbols = []
    for index, address in enumerate(addresses):
        if index % 256 == 0:
            marker = struct.unpack_from("<I", raw, 0x3d4680 + index // 256 * 4)[0]
            if marker != pos - 0x385260:
                raise ValueError("kallsyms name-marker mismatch")
        length = raw[pos]
        decoded = "".join(tokens[x] for x in raw[pos+1:pos+1+length])
        pos += 1 + length
        if not decoded or not 0xc0008000 <= address <= 0xc0467144:
            raise ValueError("Invalid kallsyms entry")
        symbols.append((address, decoded[0], decoded[1:]))
    start = raw.index(b"Linux version ")
    version = raw[start:raw.index(0, start)].decode("ascii").strip()
    wanted = {"start_kernel", "vprintk_emit", "log_store", "log_buf_addr_get",
              "log_buf_len_get", "pvc_init_early", "pvc_map_io", "pvc_smp_prepare_cpus",
              "gic_init_bases", "gic_of_init", "sp804_of_init"}
    summary = {
        "source": str(source), "source_sha256": sha,
        "gzip_offset": hex(offset), "gzip_bytes": len(data)-offset-len(decoder.unused_data),
        "kernel_bytes": len(raw), "kernel_sha256": KERNEL_SHA,
        "version": version, "symbol_count": count,
        "kernel_virtual_base": "0xc0008000", "kernel_physical_load": "0x80208000",
        "virtual_to_physical_subtract": "0x3fe00000",
        "ikconfig_marker_found": b"IKCFG_ST" in raw,
        "pl011_uart_symbol_found": any("pl011" in n or "uart" in n for _, _, n in symbols),
        "ttyama_string_found": b"ttyAMA" in raw,
        "entry_points": {name: hex(addr) for addr, _, name in symbols if name in wanted},
        "printk": {
            "log_buf_pointer_pa": "0x80693a10", "log_buf_len_pa": "0x80693a24",
            "initial_buffer_pa": "0x806ab754", "initial_buffer_length": 16384,
            "first_index_pa": "0x806aaa30", "next_index_pa": "0x806aaa40",
            "first_sequence_pa": "0x806aaa28", "next_sequence_pa": "0x806aaa20"
        },
        "board_early_writes": [
            {"pa": "0x7f03807c", "value": 3},
            {"pa": "0x7f038078", "value": 15},
            {"pa": "0x80200000", "value": "0x80217168", "phase": "smp_prepare_cpus"}
        ],
        "limitations": [
            "Static extraction and symbol recovery do not establish a successful QEMU boot.",
            "No embedded IKCONFIG was found; absence of UART symbols/strings does not establish runtime module availability.",
            "The physical mapping is specific to this kernel and the original S5 DTB."
        ]
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "kernel.raw").write_bytes(raw)
    (output / "System.map").write_text("".join("%08x %s %s\n" % s for s in symbols))
    (output / "kernel-info.json").write_text(json.dumps(summary, indent=2)+"\n")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "s5-v29-offline/unpacked-v29/29_zimage.bin")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect(args.source, args.output), indent=2))
    except (ValueError, OSError, struct.error, zlib.error) as exc:
        parser.exit(1, "Kernel inspection failed: %s\n" % exc)


if __name__ == "__main__":
    main()
