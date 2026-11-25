#!/usr/bin/env python3
"""Extract the three actual stock tables connecting timers to GIC IDs."""
import hashlib
import json
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parents[1] / "s5-v29-offline/unpacked-v29/03_program.bin"
BASE = 0x06280000


def main():
    data = SOURCE.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if digest != "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe":
        raise RuntimeError("Unexpected firmware identity")

    def word(address):
        return struct.unpack_from("<I", data, address - BASE)[0]

    # 6554cee loads the byte index table via this literal. Its -0xd0 table
    # supplies MMIO bases; -0x68 supplies logical IRQ IDs (6554d00).
    index_table = word(0x06554e38)
    mmio_table, logical_irq_table = index_table - 0xd0, index_table - 0x68
    # 658baee loads this literal, indexes by logical IRQ, then calls 651ea74.
    gic_table = word(0x0658bc60)
    rows = []
    for channel in range(26):
        hardware_index = data[index_table - BASE + channel]
        logical = word(logical_irq_table + hardware_index * 4)
        gic_id = word(gic_table + logical * 4)
        rows.append({"channel": channel, "hardware_index": hardware_index,
                     "mmio_base": hex(word(mmio_table + hardware_index * 4)),
                     "logical_irq": logical, "gic_interrupt_id": gic_id,
                     "gic_spi_input": gic_id - 32,
                     "mmio_entry_address": hex(mmio_table + hardware_index * 4),
                     "logical_irq_entry_address": hex(logical_irq_table + hardware_index * 4),
                     "gic_map_entry_address": hex(gic_table + logical * 4)})
    report = {"source": str(SOURCE), "source_sha256": digest,
              "basis": "actual table bytes plus original enable/disable code; not an arithmetic extrapolation",
              "tables": {"channel_to_hardware_index": hex(index_table),
                         "hardware_index_to_mmio": hex(mmio_table),
                         "hardware_index_to_logical_irq": hex(logical_irq_table),
                         "logical_irq_to_gic_interrupt_id": hex(gic_table)},
              "call_chain": ["0x06554cde timer start; timer ctrl+8 bit5 gates IRQ operations",
                             "0x06554d00 loads logical IRQ table; calls 0x0658bada",
                             "0x0658baee loads GIC map; 0x0658baf0 maps logical ID",
                             "0x0651ea74 enables absolute GIC ID via ISENABLER +0x100"],
              "rows": rows, "irq_delivery_runtime_validated_by_this_script": False}
    (HERE / "timer-irq-map.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
