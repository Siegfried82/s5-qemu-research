# Original 7b peer: instruction ordering recovered, ISA still unidentified

This is **offline structural analysis**, not a working coprocessor emulator.
It establishes a reproducible instruction-storage ordering and a small set of
encoding/operand relationships. No QEMU model, firmware bytes, task state,
mailbox, or ready flag was modified. No firmware was uploaded. No original peer
instruction was executed during this investigation.

The processor's ISA name, register file, instruction semantics and reset state
remain unverified. The available evidence does **not** justify saying that
normal original boot has passed this processor's handshake.

## Reproduce

```sh
python3 -B s5-emulator/qemu/peer-isa-analysis/analyze.py
```

The script checks the original `03_program.bin` SHA-256, extracts its peer code
and data in memory, and verifies equality with both existing analysis copies.
It writes only this directory's `results/` files. It uses the Python standard
library. `structural_decode()` can be imported for further offline inspection;
an `unknown_word` remains unknown, and its one-word scan step is **not** a claim
that an unknown instruction is one word long.

- Original program: `fc496d62a91f6e9b1b84807183bb805bc546c9105d2938546b58a66ad9d132fe`.
- Original 65,536-byte code: `26f6eb49c382cba94195244ed759162c21066508ec79e4e5ed42c4486ce30b99`.
- Original 131,072-byte data: `1a14e21b097bfcc2a377a851e9414732f46974f148f6bd5bd5e4cf1561e5bef2`.
- ARM source and target: code `0x06a2d918 -> 0x7b020000`; data `0x06a3d918 -> 0x7b000000`.

`results/evidence.json` contains the numeric checks; `annotations.json` gives
every displayed word's exact original blob byte offset. `annotations.txt` is a
compact view. `normalized-halfwords.txt` contains the whole analysis word stream.
It is not an image to load in QEMU: the existing original loader bytes are kept.

## Strong new evidence: four halfword lanes inside each 64-bit block

Read every original halfword as LE16, then reverse the four halfwords inside
each aligned eight-byte block:

```text
normalized_word[i] = LE16(original_code[2 * (i XOR 3) : +2])
```

This changes the first words from `0101 a700 002a a700 ...` to
`a700 002a a700 0101 a700 002e a700 01d4 ...`.

The resulting seven `a700 + operand16` pairs occur only in the leading vector
area. Six operands are `0x2e, 0x101, 0x1d4, 0x2a7, 0x37a, 0x44d`, separated
by exactly `0xd3` words. Comparing each adjacent pair of `0xd3`-word blocks:

- The reversed-four-lane stream has **99.05–99.53% same-position equality**;
  mean 99.336%, with a 117-word uninterrupted equal run in every pair.
- An exhaustive comparison of **all 24 lane permutations** makes `[3,2,1,0]`
  the unique best order. The next-best sequence-matching score is 77.44%.
- The unmodified LE16 stream's longest equal runs are only 7–11 words; merely
  swapping halfwords within each 32-bit word does not fix the structure.

This is strong evidence for the storage ordering. It does not, by itself,
identify a processor or establish its internal byte endianness: this analysis
works with numeric halfwords, and the constant/address comparisons below are
the additional evidence for their interpretation.

## Minimal candidate control-flow decoding

All PC values here are **normalized halfword indices**, not ARM addresses.
Instruction semantics below remain inferences; the script labels them
`candidate_*`, preserves raw words and never executes them.

| Observed encoding | Candidate interpretation | Supporting relationship |
| --- | --- | --- |
| `a700 u16` | Absolute transfer to a word address | Seven vector pairs; targets align with the six repeated blocks and one entry stub |
| `d7ee 3300 hi16 lo16` | Absolute transfer to a 32-bit word address | Four occurrences form coherent entry/trampoline relationships |
| `d7ea 3360 hi16 lo16` | Relative call to `PC + 4 + signed32(hi:lo)` | All 83 targets lie inside the actual nonzero code span; startup targets include short returning-looking stubs |
| `6200` | Return candidate | Ends the `1062` and `107c` callees and the repeated `0000 6200` short stubs |
| `a4eN u16` | Immediate family, selector N | Recurring constants followed by matching address families; actual register names and width not identified |
| `d7e6 db0N hi16 lo16` | Address-bearing family; store is inferred | Shared-structure address and constant relationships below |

The apparent reset chain is `word 0 -> 0x2a -> 0x1000`. This independently
agrees with the original ARM loader writing descriptor `+8 >> 1`, value
`0x1000`, to `0x7c140778`. The register's full hardware semantics remain unknown.
At `0x1000`, the apparent entry detours `0x1015 -> 0x105c -> 0x1019` before
the following call candidates:

| Call word PC | Operand | Computed target |
| --- | --- | --- |
| `102c` | `00000032` | `1062` |
| `1030` | `00000048` | `107c` |
| `1034` | `000055d4` | `660c` |
| `1038` | `0000601a` | `7056` (`0000 6200`) |
| `103c` | `00000020` | `1060` (`0000 6200`) |
| `1048` | `00003241` | `428d`, the routine containing shared initialization |
| `1050` | `00006004` | `7058` (`0000 6200`) |

At `1058`, the absolute-transfer candidate names `6691`. This is a coherent
**candidate call layout**, not a demonstrated path through executed code.

Crucial unresolved operations occur before the magic initialization:

- The first entry words `c0a6 8020` have no verified semantics.
- Earlier callees, especially `660c`, include unrecognized loops, loads,
  stores and control operations. Their return is not proven.
- In the initializer, `42aa: d7ea b360 ffff ff2a` and
  `42b0: d7ea f360 0000 26d1` yield plausible relative targets `41d8` and
  `6985`, but their conditions, link/register effects and delay slots are
  unknown. The decoder reports them as unknown control-family encodings.

An interpreter that skips those operations would not execute the original
program faithfully. This work does not introduce such an interpreter.

## Independent shared-structure cross-check

The original ARM driver checks six word-sized magic values at fixed byte
offsets from its second shared pointer. After lane normalization, six peer
address operands in the initializer all satisfy the same equation:

```text
candidate_address_operand = 0x2138 + ARM_byte_offset / 2
```

| ARM magic | ARM byte offset | Peer address operand | Encoding family |
| --- | --- | --- | --- |
| `1234` | `0160` | `21e8` | `d7e6 db00` |
| `5678` | `3208` | `3a3c` | `d7e6 db01` |
| `9012` | `40e8` | `41ac` | `d7e6 db00` |
| `3456` | `4370` | `42f0` | `d7e6 db00` |
| `7890` | `4524` | `43ca` | `d7e6 db00` |
| `5678` | `79f0` | `5e30` | `d7e6 db01` |

The fragment also repeats each of those six offsets as an immediate followed
by the same address family with operands `2138, 213a, 213c, 213e, 2140, 2142`.
Thus the region resembles a descriptor containing the exact offset table that
the ARM consumer uses. The first shared structure's separate halfword magic
has matching candidates `a4f0 1234` and `d7e6 cb10 0000 2134`:
`0x2100 + 0x68 / 2 = 0x2134`.

Other corroborating sequences are:

```text
4297: d7e6 7305 0000 2100
42bb: ed83 bdc5 2006          ; address 2006 corresponds to ARM mailbox 400c
4317: d7e6 7300 0000 2138
431b: ed83 bdc0 2008          ; address 2008 corresponds to ARM mailbox 4010
431e: a4e0 2100
4320: ed83 bdc0 2000          ; address 2000 corresponds to ARM mailbox 4000
```

The latter suggests that the first nonzero mailbox value is itself `0x2100`,
not necessarily the Boolean value 1. **These are encoding relationships and
inferred effects, not captured peer writes.** Store width, register aliases,
arithmetic flags, memory spaces and all side effects still require validation.

## Decoder and tool comparison

The active session offered no REA MCP tools and no installed `rea`, Ghidra,
Rizin or Radare2 command. Apple LLVM 21 lists ARM, AArch64, x86 and WebAssembly
targets, not the candidate DSPs. These are ordinary local-tool observations,
not results from a REA analysis session.

The public [Teakra source](https://github.com/wwylele/teakra) was downloaded and
only its standalone disassembler was compiled. The original blob was decoded
under four halfword orders; complete relevant spans are retained in
`results/teakra-reverse-*-halfwords.txt`. For the strongly supported lane order,
Teakra interprets `a700` as an add, `d7e6` as bit-reversal and `6200` as a move.
This fails to explain the independently correlated vector, call and shared
initialization structure. It supplies **no positive match to XpertTeak**.
It does not rule out every other CEVA generation or extension.

For a further narrow comparison, the [GNU MeP opcode table](https://gnu.googlesource.com/binutils-gdb/+/refs/heads/master/opcodes/mep-opc.c)
uses `7002` for return, `b000` for its short branch and `d8090000` for its long
relative call. No matching MeP decoding was established. This is a table
comparison, not a complete MeP disassembly or exclusion proof.

No compiler/ISA-name string was found by the documented scan of the code,
data and normalized code. Public-source searches for Panasonic DSP/UniPhier,
CEVA generations and opcode encodings did not yield an ISA manual matching
this binary. Manufacturer/product naming alone is not identification.

Source archive and relevant files' hashes, local compiler version and LLVM
target list are in `results/tool-inventory.json`. The downloaded Teakra license
is preserved under `references/teakra-master/LICENSE`. To rebuild that optional
comparison tool:

```sh
cd s5-emulator/qemu/peer-isa-analysis
clang++ -std=c++17 -O0 \
  -I references/teakra-master/include \
  -I references/teakra-master/include/teakra/impl \
  teakra_probe.cpp references/teakra-master/src/disassembler.cpp \
  -o teakra-probe
python3 -B analyze.py
```

## What a faithful execution attempt still needs

The next meaningful step is an independent opcode source or a verified
register/flags model that explains entry configuration, return/link behavior,
and the initializer's conditional control transfers. A future interpreter
should read original program words through the established lane adapter,
implement only validated semantics, and stop on the first unknown operation.
It must not jump directly to the magic stores, ignore unknown instructions,
or substitute a manually completed mailbox protocol.

The current result provides a small, reproducible ISA fingerprint and concrete
targets for further reverse engineering. It does not yet provide enough
semantics to run even the complete startup path truthfully.

## Follow-up: targeted primary-decoder check after normalization

Run `python3 -B s5-emulator/qemu/peer-isa-analysis/compare_decoders.py` after
building the optional Teakra probe. The exact output is retained in
`results/primary-decoder-comparison.json`.

- A positive control made from the upstream Teak encodings correctly decodes
  NOP, a branch to `0x1000`, and RET. The decoder build itself is functional.
- Five **normalized** original-code spans still decode as unrelated Teak
  arithmetic/bit-reversal/moves, splitting the observed long control patterns.
  The mismatch is not a conclusion inherited from unnormalized bytes.
- Six exact GNU MeP mask comparisons match the standard core's `beqz`, `add`
  and `movu` families. Coprocessor-extension table overlaps are retained in
  the JSON; no configured GNU MeP disassembler execution is claimed.

The useful missing artifact is now specific: the integration manual naming
the core behind `7b000000/7b020000` and `7c14071c/7c140778`, an original peer
ELF/COFF or compiler project, or that core revision's binary opcode/programmer
reference matching the listed fingerprint. CEVA-TeakLite-III/4 and UniPhier
IPP/DPP manuals are candidates **only if independent core identification
supports them**. An old Teak table or a platform marketing document is not a
substitute. No further broad search or guessed execution is warranted by
these comparison results.
