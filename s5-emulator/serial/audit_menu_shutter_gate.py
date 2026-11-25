#!/usr/bin/env python3
"""Static read-only firmware audit of the Live-View MENU/SET shutter S1/S2 gate."""
import hashlib
import json
from pathlib import Path
import struct

PROGRAM = Path("/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-v29-offline/unpacked-v29/03_program.bin")
OUT = Path("/Volumes/SATA-1TB/Projects/s5-qemu/workspace/s5-emulator/serial/menu-shutter-gate-evidence.json")
BASE = 0x06280000


def main():
    data = PROGRAM.read_bytes()
    sha = hashlib.sha256(data).hexdigest()

    def u32(addr):
        return struct.unpack_from("<I", data, addr - BASE)[0]

    # Verify 0x0667a442 struct pointer literal at 0x0667a6dc -> 0x08aa4570
    s1_state_struct = u32(0x0667a6dc)
    assert s1_state_struct == 0x08aa4570, hex(s1_state_struct)

    # Verify bank0 raw-to-logical table at ROM 0x070c939c (copied to RAM 0x08aa89e4)
    raw0, log0, raw1, log1 = struct.unpack_from("<4I", data, 0x070c939c - BASE)
    assert (raw0, log0, raw1, log1) == (0x1, 0x100, 0x2, 0x200)

    # Verify 0x06544a50 literal -> 0x7f081000 (used by 0x06544bac [sl, #0x38] = 0x7f081038)
    gpio_base = u32(0x06544e34)
    assert gpio_base == 0x7f081000, hex(gpio_base)

    evidence = {
        "scope": "Read-only static firmware audit of 03_program.bin explaining why CENTER_LIVE_HANDLER (0x06646e84) returned early at 0x06646efe -> 0x0667a442() == 1 in Live View.",
        "program_sha256": sha,
        "menu_gate": {
            "live_handler": "0x06646e84",
            "gate_callsite": "0x06646efe",
            "gate_function": "0x0667a442",
            "gate_struct": hex(s1_state_struct),
            "predicate": "[0x08aa4570 + 2] != 0 && ([0x08aa4570 + 0x14] & 1) != 0",
            "setter_function": "0x0667a60c",
            "menu_open_entry": "0x0664704c -> 0x066be02a",
        },
        "hardware_sources_causing_stuck_shutter_s1_s2": [
            {
                "name": "body_shutter_s2_gpio",
                "mmio_address": "0x7f081038",
                "bit": 4,
                "polarity": "active_low (0=pressed, 1=released)",
                "firmware_check": "0x06544bac: ldrh.w r0, [sl, #0x38]; lsls r0, r0, #0x1b; bpl #0x6544bbe (r0=1)",
                "raw_to_logical": "raw bit 1 (0x2) -> ROM table 0x070c93a4 logical 0x200 (KEY_SHUTTER_FULL) -> 0x06543ea6 forces 0x100 (KEY_SHUTTER_HALF)",
                "opt_in_qom_property": "shutter-s2-input-high",
                "opt_in_cli_flag": "--shutter-s2-input-high",
            },
            {
                "name": "wired_remote_jack_adc",
                "mmio_address": "0x7f031024",
                "adc_bank": 1,
                "adc_channel": 4,
                "buffer_address": "0x08e21150",
                "firmware_check": "0x06544a02 -> 0x06547882 -> 0x06547a0e: sample 0..0x3a returns mode 3 -> 0x06547966 returns 0x24 -> 0x0654799a asserts 0x300 (KEY_SHUTTER_HALF|FULL); sample 0x3c5..0x439 (0x3ff) returns mode 0 (unplugged)",
                "opt_in_qom_property": "adc1-ch4-sample",
                "opt_in_cli_flag": "--adc1-ch4-sample 0x3ff",
            },
        ],
    }
    OUT.write_text(json.dumps(evidence, indent=2) + "\n")
    print("Wrote", OUT)


if __name__ == "__main__":
    main()
