#!/usr/bin/env python3
"""Audit original firmware and live diagnostic evidence for scene 0x70de timezone drift."""
import hashlib
import json
import struct
from pathlib import Path
import capstone

HERE = Path(__file__).resolve().parent
EMU_ROOT = HERE.parent
WORKSPACE_ROOT = EMU_ROOT.parent
PROGRAM_PATH = WORKSPACE_ROOT / "s5-v29-offline/unpacked-v29/03_program.bin"
LIVE_TRACE_PATH = EMU_ROOT / "qa/task-timezone-drift/diag-live-trace/diag_hits.json"
TASKA_RESULT_PATH = EMU_ROOT / "qa/task-a/lens-release-high-taska-01/result.json"

BASE = 0x06280000
DATA_ROM_RAM_DELTA = 0x08aa89e4 - 0x070c939c
EXPECTED_SHA = "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"


def u16(buf, addr):
    return struct.unpack_from("<H", buf, addr - BASE)[0]


def u32(buf, addr):
    return struct.unpack_from("<I", buf, addr - BASE)[0]


def main():
    program = PROGRAM_PATH.read_bytes()
    sha = hashlib.sha256(program).hexdigest()
    assert sha == EXPECTED_SHA, f"Unexpected program SHA-256: {sha}"

    # 1. Verify ADC channel-to-MMIO pointer table (0x08aa7de8, ROM 0x070c87a0)
    tbl_ram = u32(program, 0x0659f0a8)
    cnt_rom = u32(program, 0x0659f0bc)
    assert tbl_ram == 0x08aa7de8 and cnt_rom == 0x06f48dc4
    bank_counts = list(program[cnt_rom - BASE : cnt_rom - BASE + 3])
    assert bank_counts == [12, 7, 4]
    adc_mmio = {}
    for bank, count in enumerate(bank_counts):
        rom_base = (tbl_ram - DATA_ROM_RAM_DELTA) + 4 + bank * 48
        adc_mmio[f"bank{bank}"] = [hex(u32(program, rom_base + ch * 4)) for ch in range(count)]
    assert adc_mmio["bank0"][5] == "0x7f030028"
    assert adc_mmio["bank0"][11] == "0x7f030040"
    assert adc_mmio["bank2"][0] == "0x7f032014"

    # 2. Verify resistor-ladder thresholds in 0x06545554 (ADC bank2 ch0) and 0x065454d8 (ADC bank0 ch5)
    t_up = u16(program, 0x063191ae)
    t_right = u16(program, 0x063191b0)
    t_down = u16(program, 0x063191b2)
    t_push = u16(program, 0x063191b4)
    t_max10 = u16(program, 0x063191c4)
    assert (t_up, t_right, t_down, t_push, t_max10) == (0x00b8, 0x0183, 0x025a, 0x0322, 0x03ff)

    # 3. Verify raw -> logical mapping tables for bank0 (0x08aa89e4) and bank2 (0x08aa8a9c)
    bank0_map = {}
    for i in range(23):
        raw, log = struct.unpack_from("<2I", program, (0x070c939c - BASE) + i * 8)
        bank0_map[hex(raw)] = hex(log)
    assert bank0_map["0x20"] == "0x1"  # KEY_LEFT
    assert bank0_map["0x10"] == "0x2"  # KEY_RIGHT
    assert bank0_map["0x4"] == "0x4"   # KEY_UP
    assert bank0_map["0x8"] == "0x8"   # KEY_DOWN

    bank2_map = {}
    rom_b2 = 0x08aa8a9c - DATA_ROM_RAM_DELTA
    for i in range(14):
        raw, log = struct.unpack_from("<2I", program, (rom_b2 - BASE) + i * 8)
        bank2_map[hex(raw)] = hex(log)
    assert bank2_map["0x2"] == "0x8"   # joystick DOWN -> UI key 6

    # 4. Verify repeat table entries for bank0 bit 0 (KEY_LEFT) and bank2 bit 3 (joystick DOWN)
    rom_rep0 = 0x08aa8204 - DATA_ROM_RAM_DELTA
    rep0_entry0 = struct.unpack_from("<IIHHH", program, rom_rep0 - BASE)
    assert rep0_entry0 == (0x1, 0xf, 25, 10, 0)
    rom_rep2 = 0x08aa8684 - DATA_ROM_RAM_DELTA
    rep2_entry3 = struct.unpack_from("<IIHHH", program, (rom_rep2 - BASE) + 3 * 16)
    assert rep2_entry3 == (0x8, 0xf, 25, 10, 0)

    # 5. Verify scene 0x70de timezone key handler (0x068b7742) and timezone table (0x06ee5cf0)
    assert u32(program, 0x068b7894) == 0x00010003
    tbb_targets = {
        3 + i: hex(0x068b7768 + program[0x068b7768 - BASE + i] * 2)
        for i in range(8)
    }
    assert tbb_targets[3] == "0x68b782c"  # -> 0x068b759a (decrement / westward)
    assert tbb_targets[4] == "0x68b7836"  # -> 0x068b7522 (increment / eastward)
    assert tbb_targets[5] == "0x68b7826"  # -> 0x068b75c2 (DST toggle)
    assert tbb_targets[6] == "0x68b7860"  # -> 0x0664e092 (ignored)

    tz_entries = []
    active_tz_ids = []
    for i in range(40):
        raw_entry = program[0x06ee5cf0 - BASE + i * 6 : 0x06ee5cf0 - BASE + (i + 1) * 6]
        tz_id, kind = raw_entry[0], raw_entry[1]
        offset_min = struct.unpack_from("<h", raw_entry, 2)[0]
        tz_entries.append({"index": i, "id": tz_id, "kind": kind, "offset_minutes": offset_min})
        if kind == 4 and i < 39:
            active_tz_ids.append(tz_id)
    assert len(active_tz_ids) == 32
    assert tz_entries[39]["id"] == 0 and tz_entries[39]["kind"] == 4

    # 6. Verify live diagnostic trace (if present)
    live_summary = None
    if LIVE_TRACE_PATH.exists():
        hits = json.loads(LIVE_TRACE_PATH.read_text())
        key_hits = [h for h in hits if h["name"] == "scene70de_key_handler"]
        dec_hits = [h for h in hits if h["name"] == "tz_step_dec_west"]
        inc_hits = [h for h in hits if h["name"] == "tz_step_inc_east"]
        touch_hits = [h for h in hits if h["name"] == "scene70de_touch_handler"]
        assert len(dec_hits) >= 3 and len(inc_hits) == 0 and len(touch_hits) == 0
        assert [h["r0"] for h in key_hits[:6]] == [
            "0x10006", "0x10003", "0x10006", "0x10003", "0x10006", "0x10003"
        ]
        assert [h["tz_id_08cc4872"] for h in dec_hits[:3]] == [1, 0, 38]
        first = dec_hits[0]
        assert first["adc_buf_23"][19] == 0
        assert first["adc_buf_23"][5] == 0
        assert first["drv_state_08aa8190_0_to_19"][16] == "0x20"
        assert first["drv_state_08aa8190_0_to_19"][17] == "0x2"
        assert first["cur_in_banks"][2] == "0x1"   # bank0 repeat KEY_LEFT
        assert first["cur_in_banks"][10] == "0x8"  # bank2 repeat joystick DOWN
        live_summary = {
            "trace_file": str(LIVE_TRACE_PATH.resolve()),
            "total_hits": len(hits),
            "scene70de_key_sequence": [h["r0"] for h in key_hits],
            "tz_step_dec_west_hits": len(dec_hits),
            "tz_step_inc_east_hits": len(inc_hits),
            "scene70de_touch_hits": len(touch_hits),
            "observed_tz_id_progression": [h["tz_id_08cc4872"] for h in dec_hits],
            "observed_adc_bank2_ch0_index19": first["adc_buf_23"][19],
            "observed_adc_bank0_ch5_index5": first["adc_buf_23"][5],
            "observed_adc_bank0_ch11_index11": first["adc_buf_23"][11],
            "observed_drv_state_0x40_adc2_ch0_decoded": first["drv_state_08aa8190_0_to_19"][16],
            "observed_drv_state_0x44_adc0_ch5_decoded": first["drv_state_08aa8190_0_to_19"][17],
            "observed_bank0_repeat_08ca962c": first["cur_in_banks"][2],
            "observed_bank2_repeat_08ca964c": first["cur_in_banks"][10],
        }

    taska_summary = None
    if TASKA_RESULT_PATH.exists():
        ta = json.loads(TASKA_RESULT_PATH.read_text())
        taska_summary = {
            "result_file": str(TASKA_RESULT_PATH.resolve()),
            "adc_bank2_results_at_stop": ta["adc"]["banks"][2]["results"],
            "adc_bank2_conversions": ta["adc"]["banks"][2]["conversions"],
            "adc_bank0_results_at_stop": ta["adc"]["banks"][0]["results"],
            "graphics_swaps": ta.get("graphics_swaps"),
        }

    report = {
        "scope": "Read-only static firmware audit + live read-only QEMU breakpoint trace; no firmware modification or guest UI writes.",
        "program_sha256": sha,
        "root_cause_summary": (
            "QEMU ADC bank 2 channel 0 (0x7f032014) returns 0 by default. Original 0x06544990 reads bank 2 "
            "ch 0..3 into [0x08e21130 + 0x26] and 0x06544a36 passes [0x08e21130 + 0x26] to directional "
            "resistor-ladder decoder 0x06545554. Sample 0 falls inside band 0..0x38 (0..56), returning raw "
            "mask 0x20 -> bank0 logical 0x1 (KEY_LEFT). Original repeat engine 0x06543de0 (table 0x08aa8204[0], "
            "first_delay=25, repeat_period=10) continuously fires bank0 repeat 0x1 -> 0x0663ecea..0x0663ed22 -> "
            "0x066bddc8(3, 1) -> UI event 0x0f000002 param 0x00010003 -> 0x068b7988 -> 0x068b7742 -> "
            "0x068b759a -> 0x068b754a, decrementing timezone ID [0x08cc4872] continuously (1 -> 0 -> 38 -> 37 ...). "
            "Because the 32-entry circular timezone table wraps westward faster than the ~9 s LCD refresh swap "
            "cadence, slow frame captures exhibit temporal aliasing that appears as eastward steps "
            "(Tehran GMT+3:30 -> Kabul +4:30 -> ... -> Guam/Sydney +10:00)."
        ),
        "adc_channel_table_08aa7de8": adc_mmio,
        "primary_drift_source": {
            "hardware_register": "0x7f032014",
            "adc_bank": 2,
            "adc_channel": 0,
            "logical_adc_index": 27,
            "buffer_address": "0x08e21156",
            "decoder_function": "0x06545554",
            "resistor_ladder_bands": [
                {"center": 0x0000, "half_width": 0x38, "range": [0, 56], "raw_mask": "0x20", "logical_bank0": "0x1", "key_name": "KEY_LEFT", "ui_key": 3},
                {"center": t_up, "half_width": 0x38, "range": [t_up - 0x38, t_up + 0x38], "raw_mask": "0x4", "logical_bank0": "0x4", "key_name": "KEY_UP", "ui_key": 5},
                {"center": t_right, "half_width": 0x38, "range": [t_right - 0x38, t_right + 0x38], "raw_mask": "0x10", "logical_bank0": "0x2", "key_name": "KEY_RIGHT", "ui_key": 4},
                {"center": t_down, "half_width": 0x38, "range": [t_down - 0x38, t_down + 0x38], "raw_mask": "0x8", "logical_bank0": "0x8", "key_name": "KEY_DOWN", "ui_key": 6},
            ],
            "idle_sample_above_all_bands": {
                "minimum_above_down_band": t_down + 0x38 + 1,
                "calibration_10bit_max_063191c4": t_max10,
                "recommended_explicit_sample": "0x3ff",
                "decoded_raw_mask": "0x0",
            },
            "repeat_table_entry_08aa8204_0": {
                "mask": "0x1",
                "exclusive_mask": "0xf",
                "first_delay": 25,
                "repeat_period": 10,
            },
            "scene70de_dispatch": {
                "ui_event": "0x0f000002",
                "ui_key_param": "0x00010003",
                "handler": "0x068b7742",
                "branch": "0x068b782c -> 0x068b759a -> 0x068b754a",
                "effect": "Decrements timezone ID at [0x08cc4872] through 32 active entries in table 0x06ee5cf0 (wrapping 0 -> 38).",
            },
        },
        "secondary_observed_repeat_source_not_causing_tz_drift": {
            "hardware_register": "0x7f030028",
            "adc_bank": 0,
            "adc_channel": 5,
            "buffer_address": "0x08e2113a",
            "decoder_function": "0x065454d8",
            "default_zero_result": {
                "raw_mask": "0x2",
                "logical_bank2": "0x8",
                "repeat_entry_08aa8684_3": {"mask": "0x8", "exclusive_mask": "0xf", "first_delay": 25, "repeat_period": 10},
                "ui_key_param": "0x00010006",
                "scene70de_effect": "Falls through to 0x0664e092 (ignored in scene 0x70de); does not step timezone, but will emit repeat UI key 6 (DOWN) in scenes that consume vertical navigation.",
            },
            "idle_threshold_note": f"Highest band in 0x065454d8 is 0x{t_push:04x} +/- 0x38 ({t_push - 0x38}..{t_push + 0x38}); 0x3ff (1023) is idle.",
        },
        "scene70de_tbb_targets_keys_3_to_10": tbb_targets,
        "timezone_table_active_count": len(active_tz_ids),
        "live_diagnostic_proof": live_summary,
        "task_a_reference": taska_summary,
        "limits": [
            "Raw 10-bit sample 0x3ff is established strictly against original firmware threshold bands in 0x06545554 and constant [0x063191c4]=0x03ff; physical resistor-ladder voltages are not measured.",
            "Setting adc2-ch0-sample=0x3ff addresses only ADC bank 2 channel 0; unrelated GPIOs and other ADC channels (including ADC bank 0 channel 5) remain at default 0.",
        ],
    }
    (HERE / "timezone-drift-evidence.json").write_text(json.dumps(report, indent=2) + "\n")

    cs = capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_THUMB)
    cs.detail = True
    ranges = [
        (0x06544982, 0x0654499c),  # ADC bank0/1/2 reads into 0x08e21130
        (0x06544a30, 0x06544a44),  # ADC bank2 ch0 [0x08e21130+0x26] -> 0x06545554
        (0x06545554, 0x065455a6),  # Directional 4-way resistor-ladder decoder
        (0x06544ace, 0x06544adc),  # ADC bank0 ch5 [0x08e21130+0x0a] -> 0x065454d8
        (0x065454d8, 0x06545536),  # Joystick 5-way resistor-ladder decoder
        (0x06543de0, 0x06543e6c),  # Trigger / repeat / release engine
        (0x0663ece2, 0x0663ed38),  # bank0 repeat bits 0..3 -> UI keys 3..6
        (0x0663ede6, 0x0663ee6c),  # bank2 repeat bits 0..3 -> UI keys 3..6
        (0x066bddc8, 0x066bddd4),  # UI key -> event 0x0f000002
        (0x068b7988, 0x068b79da),  # state 0x20 event handler -> scene 0x70de -> 0x068b7742
        (0x068b7742, 0x068b77bc),  # scene 0x70de UI key switch (0x10003 / 0x10004)
        (0x068b7826, 0x068b783c),  # scene 0x70de branch targets (DST / dec / inc)
        (0x068b754a, 0x068b75c2),  # timezone decrement (westward) 0x068b754a & 0x068b759a
    ]
    lines = [f"Original program SHA256={sha}; selected Thumb-2 blocks for timezone drift audit; no guest patched."]
    for start, end in ranges:
        lines.append(f"\nRANGE 0x{start:08x}..0x{end:08x}")
        for ins in cs.disasm(program[start - BASE : end - BASE], start):
            suffix = ""
            if ins.mnemonic.startswith("ldr") and len(ins.operands) > 1:
                op = ins.operands[1]
                if op.type == capstone.arm.ARM_OP_MEM and op.mem.base == capstone.arm.ARM_REG_PC:
                    lit = ((ins.address + 4) & ~3) + op.mem.disp
                    suffix = f" ; [0x{lit:08x}]=0x{u32(program, lit):08x}"
            lines.append(f"{ins.address:08x} {ins.bytes.hex():10} {ins.mnemonic:10} {ins.op_str}{suffix}")
    (HERE / "timezone-drift.disasm.txt").write_text("\n".join(lines) + "\n")
    print(json.dumps({"verified": True, "program_sha256": sha,
                      "evidence": str(HERE / "timezone-drift-evidence.json"),
                      "disasm": str(HERE / "timezone-drift.disasm.txt"),
                      "live_trace_included": live_summary is not None}, indent=2))


if __name__ == "__main__":
    main()
