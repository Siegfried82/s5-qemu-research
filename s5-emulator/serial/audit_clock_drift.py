#!/usr/bin/env python3
"""Static read-only firmware audit of ADC bank 0 ch 5 -> Clock Set hour drift."""
import hashlib
import json
from pathlib import Path
import struct

PROGRAM = Path("/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-v29-offline/unpacked-v29/03_program.bin")
OUT = Path("/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/clock-drift-evidence.json")
BASE = 0x06280000


def main():
    data = PROGRAM.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    def u32(addr):
        return struct.unpack_from("<I", data, addr - BASE)[0]

    def u16(addr):
        return struct.unpack_from("<H", data, addr - BASE)[0]

    # Verify literal pools for 0x065454d8 (ADC0 ch5 resistor-ladder decoder)
    # 0x065454d8: ldr r2, [pc, #0x1bc] -> 0x06545698 = 0x08aa8190
    # 0x065454e8: ldr r3, [pc, #0x1c0] -> 0x065456ac = 0x06319000
    latch_base = u32(0x06545698)
    cal_base = u32(0x065456ac)
    assert latch_base == 0x08aa8190, hex(latch_base)
    assert cal_base == 0x06319000, hex(cal_base)

    centers = [0, u16(cal_base + 0x1ae), u16(cal_base + 0x1b0), u16(cal_base + 0x1b2), u16(cal_base + 0x1b4)]
    raw_masks = [0x2, 0x4, 0x8, 0x1, 0x10]
    half_width = 0x38
    bands = []
    for idx, (c, mask) in enumerate(zip(centers, raw_masks)):
        lo = max(0, c - half_width)
        hi = c + half_width
        bands.append({
            "band_index": idx,
            "center": c,
            "center_hex": hex(c),
            "half_width": half_width,
            "range": [lo, hi],
            "decoded_raw_mask": hex(mask),
            "note": "JOYSTICK_DOWN -> logical bank2 0x8 -> repeat UI key 0x00010006" if idx == 0 else (
                "requires r1==0 else returns latched [0x08aa8190+0x5c]" if idx == 4 else ""
            ),
        })

    # Verify Clock Set (scene 0x2, state 0x1f) TBB table at 0x068b0eb0
    tbb_base = 0x068b0eb0
    clock_tbb = {}
    for ui_key in range(3, 19):
        b = data[tbb_base - BASE + (ui_key - 3)]
        clock_tbb[str(ui_key)] = hex(tbb_base + 2 * b)

    evidence = {
        "scope": "Read-only static firmware audit of 03_program.bin proving ADC bank 0 ch 5 -> Clock Set hour drift; no firmware modification or guest UI writes.",
        "program_sha256": sha,
        "root_cause_summary": (
            "QEMU ADC bank 0 channel 5 (0x7f030028) returns 0 by default. Original 0x06544988 reads "
            "bank 0 ch 0..11 into [0x08e21130..0x08e21146], and 0x06544ad4 passes [0x08e2113a] (ch 5) "
            "to joystick resistor-ladder decoder 0x065454d8. Sample 0 falls inside band 0 (0..56), "
            "returning raw mask 0x2 -> logical bank2 0x8 -> repeat engine entry 0x08aa8684[3] -> "
            "UI key event 0x0f000002 param 0x00010006 (DOWN repeat). In scene 0x2 (Clock Set, state 0x1f), "
            "dispatcher 0x068b0ffc -> 0x068b0e8c routes 0x00010006 at 0x068b0ee0..0x068b0eec -> "
            "0x068b0f8e -> 0x068b0a08(1), continuously decrementing the highlighted Clock Set field (Hour)."
        ),
        "hardware_source": {
            "hardware_register": "0x7f030028",
            "adc_bank": 0,
            "adc_channel": 5,
            "buffer_address": "0x08e2113a",
            "reader_callsite": "0x06544988",
            "decoder_callsite": "0x06544ad6",
            "decoder_function": "0x065454d8",
            "latch_address": hex(latch_base + 0x5c),
            "calibration_base": hex(cal_base),
            "resistor_ladder_bands": bands,
            "idle_sample_above_all_bands": {
                "minimum_above_band4": centers[-1] + half_width + 1,
                "calibration_10bit_max_063191c4": u16(cal_base + 0x1c4),
                "recommended_explicit_sample": "0x3ff",
                "decoded_raw_mask": "0x0",
            },
        },
        "clock_set_scene2_handler": {
            "state_table_slot_0x1f": "0x06a1b168",
            "event_dispatcher": "0x068b0ffc",
            "scene2_key_handler": "0x068b0e8c",
            "repeat_up_0x10005_branch": "0x068b0ee6 -> 0x068b0f78 -> 0x068b0a3a(1)",
            "repeat_down_0x10006_branch": "0x068b0eec -> 0x068b0f8e -> 0x068b0a08(1)",
            "center_key_7_confirm_branch": "0x068b0fe0 -> 0x068b0916(0)",
            "confirm_completion_modal_branch": "0x068b0990 -> 0x068147b8(0x9c)",
            "modal_0x9c_center_key_branch": "0x068b10b8 -> 0x068b1476",
            "tbb_targets_keys_3_to_18": clock_tbb,
        },
    }
    OUT.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
