# 7b coprocessor: verified bytes, unidentified ISA

The ordinary full-boot run stops in the ARM driver's wait for a separately loaded processor. No implementation here sets its ready flag, fabricates its shared structures, modifies its original program, or claims that its microcode executed. The ISA has not been identified, and no compatible emulator has been verified.

Reproduce the byte evidence and ARM host disassembly from the hash-checked original component:

```sh
s5-emulator/lab/.venv/bin/python s5-emulator/display/coprocessor-analysis/inspect_blobs.py
```

`blob-evidence.json` records original/source/target addresses, exact blob SHA-256 values, zero/nonzero counts, and aligned occurrences of handshake constants. `code.bin` and `data.bin` are exact extracted original bytes, not replacements. `arm-host-disassembly.txt` decodes only the ARM host driver; it does not pretend to decode the unknown processor.

## Original ARM-side evidence

- The task calling `0x065491e4` invokes `0x065493c0`. That constructs a descriptor and calls `0x064e6b34`, a tail branch to `0x064b12f6`.
- The descriptor loads source `0x06a2d918`, length `0x10000`, to `0x7b020000`, and source `0x06a3d918`, length `0x20000`, to `0x7b000000`. The original `0x064b12f6` copies words with ARM instructions; this is not an unimplemented DMA completion.
- Controller index 0 uses `0x7c14071c` (index 1 uses `0x7c140720`). The driver asserts bits 2/3 and then bit 1, copies the programs, and clears bit 3. It writes descriptor field `+8 >> 1` (`0x1000` for this descriptor) to `0x7c140778`. Its precise hardware meaning and the coprocessor reset PC remain unverified.
- `0x065491ee` repeatedly loads `0x7b004000` until nonzero. This is not evidence for a simple PLL-ready status bit.
- Afterward, ARM reads `0x7b00400c` and `0x7b004010`, calculating two pointers as `0x7b000000 + value * 2`. It validates shared-data constants, including first pointer `+0x68 == 0x1234`, and second pointer `+0x160 == 0x1234`, `+0x3208 == 0x5678`, `+0x40e8 == 0x9012`, `+0x4370 == 0x3456`, `+0x4524 == 0x7890`, and `+0x79f0 == 0x5678`. Widths differ: the first check is halfword; those listed for the second pointer are word reads.
- The next handshake writes `0x7b004004 = 1` and waits for `0x7b004008 != 0`. Correctly mapped SRAM alone cannot execute the separate program or produce this protocol.

The initial data blob contains only 250 nonzero bytes. Its mailbox region is zero, and none of the five checked constants occurs as an aligned LE16 or LE32 value anywhere in it. The code blob contains all five constants, clustered near offsets `0x8572..0x85da`; that supports investigating initialization code but is **not an ISA-level proof of any particular store instruction**. Do not replace this protocol by writing the expected values.

Observed full-boot evidence lives in `../../menu-smp/results/pulse-timer-60s/`; the kernel agent identified the current task as `uiTSK066`. A separate experiment temporarily delaying that task through the original RTOS may exercise the real display task, but is not evidence that this coprocessor or normal full boot works.

## Bounded public-source investigation

No firmware bytes were uploaded. Public-source queries covered Panasonic/UniPhier IPP/DPP architecture, the camera SoC name `pvc03v`, and available DSP emulators. Sources establish the following limited facts:

- Panasonic's own [ASPDAC 2011 slides, “Advanced System LSIs for Home 3D Systems”](https://www.aspdac.com/aspdac2011/archive/pdf/8D-1.pdf) describe UniPhier IPP and DPP processors with separate instruction/data memories and microcode. The [Panasonic Technical Journal article](https://tech.panasonic.com/jp/phd/pdf/technology-journal/v5702/p0205.pdf) provides related platform context. Neither inspected source gives an opcode table matching this S5 blob or identifies this specific `0x7b` block. **IPP/DPP remain background candidates, not an identification.**
- The authors' [ASPDAC 2007 paper](https://www.cecs.uci.edu/~papers/aspdac07/pdf/p649_6D-4.pdf) describes UniPhier platform and low-power architecture, not a matching ISA manual.
- [Teakra](https://github.com/wwylele/teakra) is an actual open-source emulator for the DSi/3DS Xpert-Teak processor. Its original [decoder](https://raw.githubusercontent.com/wwylele/teakra/master/src/decoder.h) defines `nop = 0x0000`, branch base `0x4180`, and `bitrev_ebrv` base `0xd7e0`. No coherent boot/control-flow decoding of this blob was demonstrated against that table. Teakra has **not** been built, executed on this blob, or established compatible. This does not exclude every CEVA generation.
- The inspected original UniPhier Linux sources expose clock/platform support, not a demonstrated interpreter for this core. A bounded search failing to locate a matching emulator does not prove that none exists.

Next useful evidence would be a processor/ISA manual, compiler/disassembler metadata, or a consistent decoding of reset flow and shared-memory initialization. Until then, restoring storage, ARM scheduling, and SRAM is valid progress, but reporting coprocessor readiness would be fabricated.
