#!/usr/bin/env python3
"""Offline structural analysis of the original 7b peer. Never executes it.

The names `candidate_*` below are hypotheses, NOT an identified ISA decoder.
Unknown words stay unknown; apparent instruction lengths are only accepted for
the small recurring families listed here. No VM or mailbox writing is present.
"""
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import itertools
import json
from pathlib import Path
import re
import struct
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROGRAM = ROOT.parent / "s5-v29-offline/unpacked-v29/03_program.bin"
PROGRAM_SHA = "fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe"
CODE_SHA = "26f6eb49c382cba94195244ed759162c21066508ec79e4e5ed42c4486ce30b99"
DATA_SHA = "1a14e21b097bfcc2a377a851e9414732f46974f148f6bd5bd5e4cf1561e5bef2"
BASE = 0x06280000


def sha(b):
    return hashlib.sha256(b).hexdigest()


def words(blob, group=4):
    raw = struct.unpack("<%dH" % (len(blob) // 2), blob)
    return [raw[i // group * group + group - 1 - i % group]
            for i in range(len(raw))]


def signed32(x):
    return x - 0x100000000 if x & 0x80000000 else x


def physical_offsets(pc, size):
    # Each reported logical word maps exactly to these original file bytes.
    return [hex(2 * ((pc + i) ^ 3)) for i in range(size)]


def structural_decode(w, pc):
    """Return one observed encoding family, or one UNKNOWN WORD.

    This is an annotation format; an unknown word's size=1 does NOT prove that
    it is a one-word instruction. Subsequent pattern matches remain tentative.
    """
    x = w[pc]
    n = 1
    out = {"word_pc": hex(pc), "role": "unknown_word", "semantics_verified": False}
    if x == 0xa700 and pc + 1 < len(w):
        n = 2
        out.update(role="candidate_absolute_transfer16", target_word=hex(w[pc + 1]))
    elif x in (0xd7e5, 0xd7e6, 0xd7e7, 0xd7e9, 0xd7ea, 0xd7ee) and pc + 3 < len(w):
        n = 4
        sub, imm = w[pc + 1], w[pc + 2] << 16 | w[pc + 3]
        out.update(role="unknown_prefix32_operand32", prefix=hex(x),
                   subopcode=hex(sub), operand32=hex(imm))
        if (x, sub) == (0xd7ee, 0x3300):
            out.update(role="candidate_absolute_transfer32", target_word=hex(imm))
        elif (x, sub) == (0xd7ea, 0x3360):
            out.update(role="candidate_relative_call32", displacement_words=signed32(imm),
                       target_word=hex(pc + 4 + signed32(imm)),
                       continuation_word=hex(pc + 4))
        elif x == 0xd7ea:
            # Show arithmetic without guessing conditions, call vs branch,
            # delay slots, register effects, or even a complete opcode mask.
            out.update(role="unknown_relative_control_family",
                       candidate_target_word=hex(pc + 4 + signed32(imm)),
                       displacement_words=signed32(imm))
    elif x & 0xffe0 == 0xa4e0 and pc + 1 < len(w):
        n = 2
        out.update(role="candidate_immediate16_family", selector=hex(x & 31),
                   operand16=hex(w[pc + 1]))
    elif x in (0xed16, 0xed83, 0xedb0) and pc + 2 < len(w):
        n = 3
        out.update(role="unknown_prefix16_operand16", prefix=hex(x),
                   subopcode=hex(w[pc + 1]), operand16=hex(w[pc + 2]))
    elif x == 0x6200:
        out.update(role="candidate_return_word")
    elif x == 0:
        out.update(role="zero_word_possible_nop_or_padding")
    out.update(words=n, raw_words=[hex(v) for v in w[pc:pc + n]],
               original_blob_byte_offsets=physical_offsets(pc, n))
    return out


def annotate(w, first, end):
    rows = []
    while first < end:
        d = structural_decode(w, first)
        rows.append(d)
        first += d["words"]
    return rows


def strings(blob, minsize=8):
    return [{"byte_offset": hex(m.start()), "text": m.group().decode("ascii")}
            for m in re.finditer(rb"[ -~]{%d,}" % minsize, blob)]


def main():
    program = PROGRAM.read_bytes()
    assert sha(program) == PROGRAM_SHA, "Original program identity changed"
    code = program[0x06a2d918 - BASE:0x06a3d918 - BASE]
    data = program[0x06a3d918 - BASE:0x06a5d918 - BASE]
    assert sha(code) == CODE_SHA and sha(data) == DATA_SHA
    assert code == (ROOT / "serial/embedded-peer-code.bin").read_bytes()
    assert code == (ROOT / "display/coprocessor-analysis/code.bin").read_bytes()
    assert data == (ROOT / "serial/embedded-peer-data.bin").read_bytes()
    assert data == (ROOT / "display/coprocessor-analysis/data.bin").read_bytes()
    w = words(code)
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    report = {
        "scope": "Offline analysis only; no QEMU execution or model/state changes",
        "isa_identified": False,
        "interpreter_implemented": False,
        "source": str(PROGRAM), "program_sha256": sha(program),
        "code_sha256": sha(code), "data_sha256": sha(data),
        "code_bytes": len(code), "data_bytes": len(data),
        "normalization": "normalized_word[i] = LE16(code[2 * (i XOR 3): +2]); reverse four halfwords inside each aligned 8-byte block",
        "normalization_status": "strong structural inference, not confirmed by ISA or hardware documentation",
        "normalized_le16_sha256": sha(struct.pack("<%dH" % len(w), *w)),
        "normalized_artifact_use": "analysis-only; never substitute for original loader input",
        "lane_order_comparison": [],
        "vector_candidates": [],
        "absolute_transfer_candidates": [],
        "relative_call_candidates": [],
    }
    starts = [0x2e, 0x101, 0x1d4, 0x2a7, 0x37a, 0x44d]
    for group in (1, 2, 4, 8):
        v = words(code, group)
        pairs = []
        for a, b in zip(starts, starts[1:]):
            sm = SequenceMatcher(None, v[a:a + 0xd3], v[b:b + 0xd3], autojunk=False)
            longest = sm.find_longest_match()
            pairs.append({"a_word": hex(a), "b_word": hex(b), "compared_words": 0xd3,
                          "sequence_match_ratio": sm.ratio(),
                          "longest_equal_run_words": longest.size,
                          "longest_a_word": hex(a + longest.a),
                          "longest_b_word": hex(b + longest.b)})
        report["lane_order_comparison"].append({"reverse_halfword_group_size": group, "pairs": pairs})
    raw = words(code, 1)
    permutations = []
    for perm in itertools.permutations(range(4)):
        v = [raw[i // 4 * 4 + perm[i % 4]] for i in range(len(raw))]
        pair_scores, direct_scores, longest_runs = [], [], []
        for a, b in zip(starts, starts[1:]):
            x, y = v[a:a + 0xd3], v[b:b + 0xd3]
            sm = SequenceMatcher(None, x, y, autojunk=False)
            pair_scores.append(sm.ratio())
            direct_scores.append(sum(xx == yy for xx, yy in zip(x, y)) / len(x))
            longest_runs.append(sm.find_longest_match().size)
        permutations.append({"lane_permutation": perm,
                             "mean_sequence_match_ratio": sum(pair_scores) / 5,
                             "mean_same_position_equal_ratio": sum(direct_scores) / 5,
                             "longest_equal_runs_words": longest_runs})
    permutations.sort(key=lambda x: x["mean_sequence_match_ratio"], reverse=True)
    assert permutations[0]["lane_permutation"] == (3, 2, 1, 0)
    report["all_24_halfword_lane_permutations"] = permutations
    for i in range(len(w) - 4):
        if w[i] == 0xa700:
            report["vector_candidates"].append(structural_decode(w, i))
        if w[i:i + 2] == [0xd7ee, 0x3300]:
            report["absolute_transfer_candidates"].append(structural_decode(w, i))
        if w[i:i + 2] == [0xd7ea, 0x3360]:
            report["relative_call_candidates"].append(structural_decode(w, i))
    assert len(report["vector_candidates"]) == 7
    assert len(report["absolute_transfer_candidates"]) == 4
    assert len(report["relative_call_candidates"]) == 83
    assert all(0 <= int(row["target_word"], 16) < 0x705c
               for row in report["relative_call_candidates"])
    report["relative_call_targets_within_nonzero_code_extent"] = 83

    # Cross-check the operand fields against the independently decoded ARM
    # consumer. No register or memory effects are executed or assumed here.
    checks = [
        (0x1234, 0x0160, 0x21e8, 0),
        (0x5678, 0x3208, 0x3a3c, 1),
        (0x9012, 0x40e8, 0x41ac, 0),
        (0x3456, 0x4370, 0x42f0, 0),
        (0x7890, 0x4524, 0x43ca, 0),
        (0x5678, 0x79f0, 0x5e30, 1),
    ]
    report["shared_structure_crosschecks"] = []
    for magic, byteoffset, address, selector in checks:
        hits = [i for i in range(0x42b7, 0x4317)
                if w[i:i + 4] == [0xd7e6, 0xdb00 + selector, 0, address]]
        assert len(hits) == 1
        assert address - byteoffset // 2 == 0x2138
        report["shared_structure_crosschecks"].append({
            "arm_expected_magic": hex(magic), "arm_byte_offset": hex(byteoffset),
            "candidate_address_operand": hex(address), "derived_word_base": "0x2138",
            "matched_encoding_word_pc": hex(hits[0]), "subopcode": hex(0xdb00 + selector),
            "relationship": "address_operand = 0x2138 + ARM_byte_offset / 2",
            "store_semantics_verified": False,
        })
    report["descriptor_offsets"] = []
    for j, value in enumerate((0x160, 0x3208, 0x40e8, 0x4370, 0x4524, 0x79f0)):
        pattern = [0xa4e0, value, 0xd7e6, 0xdb00, 0, 0x2138 + 2 * j]
        hits = [i for i in range(0x42f0, 0x4318) if w[i:i + len(pattern)] == pattern]
        assert len(hits) == 1
        report["descriptor_offsets"].append({"word_pc": hex(hits[0]),
                                             "immediate": hex(value),
                                             "address_operand": hex(0x2138 + 2 * j)})
    report["entry_link"] = {
        "arm_driver": "0x064b12f6 copies original code/data; descriptor+8 >> 1 is written to 0x7c140778",
        "arm_written_value": "0x1000",
        "candidate_vector_chain": ["word 0 -> 0x2a", "word 0x2a -> 0x1000"],
        "candidate_entry_detour": ["word 0x1015 -> 0x105c", "word 0x105c -> 0x1019"],
        "candidate_initializer_call": "word 0x1048 + 4 + 0x3241 = word 0x428d",
        "status": "mutually consistent static relationships; actual reset PC and execution remain unverified",
    }
    report["unresolved_before_magic"] = [
        {"word_pc": "0x1000", "words": ["0xc0a6", "0x8020"],
         "reason": "Unknown first entry operation; execution cannot faithfully start yet"},
        {"word_pc": "0x1034", "candidate_target": "0x660c",
         "reason": "Earlier callee includes unknown data/loop/control semantics; return not proven"},
        {"word_pc": "0x42aa", "words": ["0xd7ea", "0xb360", "0xffff", "0xff2a"],
         "candidate_target": "0x41d8",
         "reason": "Transfer type, condition, link/register effects and delay slots unknown"},
        {"word_pc": "0x42b0", "words": ["0xd7ea", "0xf360", "0x0000", "0x26d1"],
         "candidate_target": "0x6985",
         "reason": "Transfer type, condition, link/register effects and delay slots unknown"},
    ]
    report["prefix_inventory"] = [{"word": hex(v), "count": n}
                                  for v, n in Counter(w).most_common(30)]
    token_re = re.compile(r"(?i)teak|ceva|dsp|ipp|dpp|mn10|mn1[03]|compiler|microcode|coproc|ELF")
    report["compiler_string_candidates"] = [s for s in strings(code, 5) if token_re.search(s["text"])]
    report["data_compiler_string_candidates"] = [s for s in strings(data, 5) if token_re.search(s["text"])]
    report["normalized_compiler_string_candidates"] = [s for s in strings(struct.pack("<%dH" % len(w), *w), 5) if token_re.search(s["text"])]

    regions = {"vectors": (0, 0x50), "entry": (0x1000, 0x1062),
               "entry_callee_1062": (0x1062, 0x107c), "entry_callee_107c": (0x107c, 0x1081),
               "initializer": (0x428d, 0x4342), "callee_7056": (0x7056, 0x705a)}
    annotated = {name: annotate(w, *bounds) for name, bounds in regions.items()}
    (out / "annotations.json").write_text(json.dumps(annotated, indent=2) + "\n")
    lines = ["STRUCTURAL CANDIDATES ONLY. Unknown words are NOT proven one-word instructions.",
             "PC units: normalized 16-bit words. Original bytes remain unchanged."]
    for name, rows in annotated.items():
        lines += ["", name + ":"]
        for row in rows:
            raw = " ".join(f"{int(x, 16):04x}" for x in row["raw_words"])
            extras = " ".join(f"{k}={row[k]}" for k in ("target_word", "candidate_target_word", "operand32", "operand16", "selector") if k in row)
            lines.append(f"{row['word_pc']:>6}: {raw:<24} {row['role']} {extras}")
    (out / "annotations.txt").write_text("\n".join(lines) + "\n")
    (out / "normalized-halfwords.txt").write_text("\n".join(
        f"{i:04x}: " + " ".join(f"{v:04x}" for v in w[i:i + 8])
        for i in range(0, len(w), 8)) + "\n")
    (out / "evidence.json").write_text(json.dumps(report, indent=2) + "\n")

    probe = HERE / "teakra-probe"
    if probe.exists():
        for group in (1, 2, 4, 8):
            text = []
            for first, end in ((0, 0x40), (0x1000, 0x1062), (0x42b7, 0x4325)):
                result = subprocess.run([str(probe), str(ROOT / "serial/embedded-peer-code.bin"),
                                         hex(first), hex(end), str(group)],
                                        check=True, capture_output=True, text=True)
                text.append(result.stdout)
            (out / f"teakra-reverse-{group}-halfwords.txt").write_text("\n".join(text))
    print(json.dumps({"verified_original_bytes": True, "isa_identified": False,
                      "lane_normalization": "4 x LE16 reversed per 64-bit block",
                      "shared_structure_relations": len(checks),
                      "descriptor_offset_relations": len(report["descriptor_offsets"]),
                      "relative_control_candidates": len(report["relative_call_candidates"]),
                      "output": str(out)}, indent=2))


if __name__ == "__main__":
    main()
