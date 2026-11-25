#!/usr/bin/env python3
"""Decode S5 V2.9 Linux printk records from a QEMU physical RAM snapshot.

This reads recorded kernel output. It does not synthesize boot messages.
Example: QEMU HMP `pmemsave 0x80200000 0x2f00000 /absolute/path/ram.bin`.
"""
import argparse
import json
from pathlib import Path
import struct

VA_PA_DELTA = 0x3fe00000
BANNER = b"Linux version 3.18.18 (img.yamashita.seiya@imglinux125)"


def read_kmsg(data, base=0x80200000):
    def read(pa, size):
        pos = pa-base
        if pos < 0 or pos+size > len(data):
            raise ValueError("RAM snapshot does not cover physical 0x%x..0x%x" % (pa, pa+size))
        return data[pos:pos+size]

    def u32(pa):
        return struct.unpack("<I", read(pa, 4))[0]

    def u64(pa):
        return struct.unpack("<Q", read(pa, 8))[0]

    if read(0x8054b06c, len(BANNER)) != BANNER:
        raise ValueError("Snapshot does not contain the expected S5 V2.9 kernel banner at its load address")
    buf_va = u32(0x80693a10)
    buf_len = u32(0x80693a24)
    first = u32(0x806aaa30)
    last = u32(0x806aaa40)
    seq = u64(0x806aaa28)
    next_seq = u64(0x806aaa20)
    if not 0xc0000000 <= buf_va < 0xd0000000:
        raise ValueError("Invalid log_buf pointer; kernel may not have initialized, or image/version does not match")
    if not 16 <= buf_len <= 16*1024*1024 or buf_len & (buf_len-1):
        raise ValueError("Invalid log_buf length")
    if first >= buf_len or last > buf_len or not seq <= next_seq <= seq+buf_len//16:
        raise ValueError("Invalid printk cursor/sequence metadata; snapshot may be inconsistent")
    buffer = read(buf_va-VA_PA_DELTA, buf_len)
    records = []
    idx = first
    while seq < next_seq:
        if idx+16 > buf_len:
            raise ValueError("Record header extends past ring buffer")
        timestamp, length, text_len, dict_len, facility, flags = struct.unpack_from("<QHHHBB", buffer, idx)
        if length == 0:
            idx = 0
            timestamp, length, text_len, dict_len, facility, flags = struct.unpack_from("<QHHHBB", buffer, idx)
        if length < 16 or length % 4 or idx+length > buf_len or 16+text_len+dict_len > length:
            raise ValueError("Invalid printk record at index 0x%x; snapshot may be inconsistent" % idx)
        records.append({"sequence": seq, "timestamp_ns": timestamp, "level": flags >> 5,
                        "facility": facility,
                        "text": buffer[idx+16:idx+16+text_len].decode("utf-8", "replace")})
        idx += length
        seq += 1
    if idx != last:
        raise ValueError("Decoded end index does not match printk metadata; snapshot may be inconsistent")
    return {"buffer_physical_address": hex(buf_va-VA_PA_DELTA), "buffer_bytes": buf_len,
            "first_index": first, "next_index": last, "first_sequence": u64(0x806aaa28),
            "next_sequence": next_seq, "records": records}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("snapshot", type=Path)
    p.add_argument("--ram-base", type=lambda x: int(x, 0), default=0x80200000)
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    try:
        result = read_kmsg(a.snapshot.read_bytes(), a.ram_base)
    except (OSError, ValueError, struct.error) as exc:
        p.exit(1, "Cannot decode S5 printk snapshot: %s\n" % exc)
    if a.json:
        text = json.dumps(result, ensure_ascii=False, indent=2)+"\n"
    else:
        text = "".join("[%12.6f] %s\n" % (r["timestamp_ns"]/1e9, r["text"]) for r in result["records"])
    if a.output:
        a.output.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
