# S5 RTOS SMP research machine

`s5-rtos` is separate from the existing `s5-linux` machine. It executes four
actual Cortex-A7 CPU instances in QEMU TCG with MPIDR affinity 0–3. All CPUs
share one RAM allocation; overlapping low and high mappings are coherent aliases.
This is a partial research machine, not a complete camera hardware model.

Build from the emulator directory with `bash toolchain/build.sh`. Select it
with `toolchain/build/qemu-system-arm -M s5-rtos -smp 4 -m 512M -S ...`.
RAM capacity defaults to 512 MiB and is limited to 2 GiB. Both are explicit
experiment capacities rather than verified measurements of the camera.
`-m 2G` maps the complete backing at `0x80000000..0xffffffff`, with only its
first 1 GiB also aliased at `0x00000000..0x3fffffff`. Smaller configurations
map their full size through both aliases. The low `0x40000000..0x7fffffff`
region is never populated with this DRAM; existing SRAM/MMIO remains separate.
Load original components into their low addresses only. The high alias sees
the same bytes, including subsequent scatterload and runtime writes.

| Address | Model |
| --- | --- |
| `0x00000000` | Coherent low alias of the first `min(-m, 1 GiB)` bytes |
| `0x80000000` | Complete RAM backing, size given by `-m` (maximum 2 GiB) |
| `0x70001000 + cpu * 0x8000` | Shared GICv2 distributor and aliases, 448 interrupt IDs |
| `0x70002000 + cpu * 0x8000` | Fixed GIC CPU interface for each of four CPUs |
| `0x70028000..0x7002bfff` | SRAM covering observed pool/list writes, the 6144-byte original `dram_sleep` component and context through `0x7002a3f0` |
| `0x7b000000..0x7b02ffff` | Backing storage for original coprocessor data/code uploads; no coprocessor execution or fabricated ready handshake |
| `0x7c160000..0x7c160fff` | Partial module control/reset request model |
| `0x7f020000..0x7f02c000`, stride `0x1000` | 13 SP804 pairs, 27 MHz; second timer at `+0x20`; separate SPI inputs 4–29 (GIC IDs 36–61) |
| `0x7f030000`, `0x7f031000`, `0x7f032000` | Synchronous ADC banks with 12, 7, 4 channels |
| `0x7f040000..0x7f046000`, stride `0x1000` | Seven PL022-compatible SSP channels; RTC peer on channel 3, strict data gate on other channels |
| `0x7f081c00` | Captured SPI0 encoded chip-select writes |
| `0x7f050000..0x7f054000`, stride `0x1000` | Five PL011 UARTs; GIC IDs 85, 86, 294, 295, 296 |
| `0x7f058000..0x7f058400`, stride `0x100` | Five I2C controller error models; GIC IDs 282–286 |
| `0x7f081c0c` | Observed GPIO encoded RTC chip-select writes |
| `0x7f080094`, `0x7f080410` | Narrow startup-input windows; explicit `inactive-inputs` profile supplies bit 4 only |
| Other `0x70000000..0x7fffffff` | Read zero / discard write with unmodeled-access log |

The 2-GiB experiment is motivated by original program table `0x06ff11b0`:
its two nonzero rows each span 1 GiB, with CPU high mappings at `0x80000000`
and `0xc0000000`. The second row lacks a low/cached mapping (`0xdead`).
The original arena initialization supplies base `0x0fc00000` and size
`0x703e0000`; a later original allocation `0x7d159000` converts to
`0xfd159000`. The observed original guard write at `0xffa91000` was an
external write abort with the smaller backing, not a missing MMU section.
See `serial/mode35-fault-evidence.json` for the original call/translation
chain. The new mapping supplies distinct storage without masking pointers
back into 512 MiB, modifying allocator limits or changing faulting code.
Read-only `/machine` property `ram-layout-json` reports the selected backing
and alias sizes and explicitly marks physical camera capacity unverified.

The four generic timer outputs connect to standard GIC PPIs 30, 27, 26, 29.
`apply_qemu_patches.py` adds individual output lines to QEMU's SP804 while
preserving combined output 0 and existing `s5-linux` behavior. Original
firmware tables `0x06ff2a50`, `0x06ff2ab8`, and `0x06ff1240` establish the
26 timer addresses and IRQ mapping.
The extracted source entries and call chain are recorded in
`serial/timer-irq-map.json`; `serial/extract_timer_irq_map.py` regenerates that
record from the hash-checked original program.
It also adds the opt-in `s5-inferred-pulse-bit4` timer property, enabled only
by `s5-rtos`. Original RTOS configuration forces control bit 4, the original
loader selects GIC edge triggering, and the complete timer handler does not
write INTCLR. A stock level output therefore stops providing new edges after
the first expiry. The explicitly inferred S5 extension emits one pulse at
each actual timer expiry when bit 4 and interrupt enable are set. Raw and
masked interrupt status remain latched. Standard SP804 behavior remains the
default, including for `s5-linux`; no firmware instruction or GIC trigger
configuration is changed. The smoke check observes five successive 1 ms
timer edges without writing the timer INTCLR.
The original-firmware dispatcher probe provides a separate dynamic check:
`qemu/results-original-timer-pulse-irqs/irq-dispatches.json` records 30 actual
IRQ 36 dispatches after two SGIs in its first 32 samples. The corresponding
probe before this extension did not show a continuing timer tick. This
validates delivery to original handlers, not menu completion.

`load-rearms-oneshot` is another SP804 property, default false and enabled
only by `s5-rtos`. It implements the documented LOAD retrigger of an enabled
one-shot timer (ARM DDI 0271D, section 2.2.6,
[official manual](https://documentation-service.arm.com/static/5e8e2102fd977155116a4aef)).
QEMU 9.2's underlying one-shot `ptimer` disables itself on expiry; its old
LOAD handler reloads the count but does not restart it. Original S5 function
`0x06759544` configures timer 6 once, then `0x06554ca2` rearms later stages by
writing only LOAD at `0x06554cd0`. Without the opt-in fix, the original
startup sequence stops after one consumed queue-`0x16` message. The focused
`test_oneshot_reload.py` records that failure and three successful LOAD-only
expiries after the fix in `oneshot-reload-{baseline,fixed}/verification.json`.
Disabled timer writes remain stopped, periodic timers continue expiring,
and the unchanged `s5-linux` timer is tested as a control. This change neither
clears original startup flags nor produces an interrupt without a timer expiry.

UART mapping follows original tables `0x08ab4778`, `0x08ab478c` and
`0x06ff1240`. When no user serial backend is attached, each UART captures
actual transmitted bytes in a local 1 MiB ring buffer named `s5-uart0` through
`s5-uart4`. Read them with QMP `ringbuf-read` (`device`, `size`, `format=utf8`);
reading consumes the captured bytes. The model does not invent received
characters or a remote device response.
The SPI3 interrupt number is not established and remains unwired. CPU reset
state is ordinary QEMU reset state. No firmware handshakes are automatically
cleared by this board. `menu-smp/boot.py` supplies its documented experimental
VFP/MMU state, runs original RTOS entries on CPU0–2 and parks CPU3; it does not
claim that Linux is running on the parked core.

The RTC peer implements the observed three-byte commands: a big-endian
16-bit register address whose bit 0 selects read, followed by a data byte.
`0x10000001` and `0x10000000` GPIO writes select and deselect the peer. This is
an internal logical mapping, not a claim about physical pin polarity.
There is no unproven address auto-increment. Reads return the third byte;
the first two response bytes are `0xff`, which the observed caller ignores.

RTC reset assumptions are explicit: BCD time bytes `0402..040c` start at zero,
`040e=1`, `0410=0`, backup bytes `0420..043e=0`, and synchronization/status
`0440=0`. These are not measured hardware reset values. Unknown initial reads
return zero and are separately counted and logged. A firmware write is stored
for subsequent readback; this does not implement undocumented side effects.
The clock does not advance and the physical chip has not been identified.
No date or successful initialization is forced by the model.

The ADC model follows the synchronous original driver at `0x0659ed98` and
the contract in `adc/adc-contract.json`. Writes select a channel mask and
sample count, then set start bit 0. The model samples selected inputs into
result registers and clears only the documented busy bits immediately.
The `/machine` integer property `adc11-sample` defaults to zero and supplies
an explicit raw 16-bit input; the result updates only after a conversion.
Other inputs remain zero. `adc-state-json` records controls, masks, counts,
results, conversions and unsupported accesses. Conversion latency, voltage
scaling and asynchronous interrupt modes are not established. An experimental
raw input of `0x300` is not described as a physical voltage. Original threshold
tests and event handling remain firmware code.

The original function at `0x06548ed2` reads both startup GPIO windows with
`LDRH` and debounces bit 4 ten times. The `inactive-inputs` machine property
defaults to true, presenting bit 4 high at both addresses and other bits zero.
The physical input names and real reset levels are not established. No RTOS
event is modified and no interrupt is synthesized. Use
`-M s5-rtos,inactive-inputs=off` or QMP `qom-set` on `/machine` to reproduce the
earlier zero-input profile. Both 16-bit and 32-bit reads are supported.
The surrounding GPIO words also accept configuration writes in original code;
this narrow input profile does not implement those configuration semantics.

SPI0 has an unidentified peer. The default strict data gate records its first
transmit and requests a debugger stop without enqueueing an RX response.
`spi0-state-json` reports the access, chip-select value and unsupported stop.
The opt-in `/machine` boolean property `spi0-open-bus=true` forwards data
accesses to a real PL022 with an empty SSI bus. QEMU then returns zero for each
transfer, representing an explicitly assumed low MISO line, not a successful
device acknowledgement. The original firmware is allowed to handle this
absent-peer result itself. This is an exploratory comparison, not an
implementation of the unknown coprocessor.
The same strict and opt-in absent-peer mechanism applies to SPI1, SPI2, SPI4,
SPI5 and SPI6. `spi6-open-bus` and `spi6-state-json` mirror the SPI0 interface;
`spi-state-json` reports all channels. `spi-open-bus-mask` selects experimental
absent-peer channels by bit (allowed mask `0x77`); channel 3 retains the RTC
peer. The observed original LCD initialization uses SPI6 and ignores received
bytes, but passing those write transactions alone does not establish LCD
device execution or a rendered frame. The encoded SPI6 chip-select writes at
`0x7f083018` remain observable in unmodeled-access records.

The I2C model uses an explicit `generic-controller-error` profile, enabled by
default through `/machine` boolean `i2c-generic-error`. A real transmit or
receive START write (`STAT` high nibble `f` or `b`) sets observed error bit 3
and controller pending bit 4. Interrupt delivery follows enable bit 5. The
original ISR and worker handle the error and retries; cleanup clears pending
and IRQ. Full 32-bit control state preserves the observed bit 8. No successful
ACK or peer data is produced. The physical error category/cause is not known,
so this is not labeled a NACK model. Disabling the property leaves transactions
without modeled completion. `i2c-state-json` reports every bus's registers,
START/error/IRQ counts and unsupported accesses.

Although the register layout resembles QEMU's Exynos I2C controller, original
S5 ISR tests showed that changing status bit 0 alone does not invoke its error
path. Reusing that controller's empty-bus NACK would incorrectly allow normal
progress in these handlers. The selected error branch is supported by the
original-ISR tests in `i2c/error-isr-check-v2/verification.json` (16 error cases
and 8 bit-0 control cases). This identifies a firmware error path, not the
underlying chip vendor or an implemented external device.

The module request model follows only the table and instruction evidence in
`menu-smp/module-handshake-evidence.json`. At the 32 control offsets, request
bits 4–7 reflect immediately into status bits 0–3. At the 26 reset offsets,
request bit 0 reflects into bits 4 and 8. Other bits are preserved, with no
fabricated status at bits 16 or 24. Unknown offsets retain writes but produce
unsupported-access logs. Real completion delays and error states are unknown.

Use `-d unimp -D trace.log` for protocol and unsupported-access records. Query
QMP `qom-get` at `/machine/rtc-peer`, property `state-json`, for RTC counters,
unsupported registers and defined register values; `/machine`, property
`module-state-json`, reports module requests and unsupported reads/writes.
These properties return JSON strings. GDB debug reads/writes of MMIO may split
into byte accesses; use guest load/store instructions or these state properties
when access width matters. ARM QEMU data watchpoints have stalled this setup;
the runners use instruction breakpoints instead.
`/machine`, property `gic-state-json`, safely exposes distributor and CPU
control, priority, interrupt targets/pending state and CPU halted status.
Do not read banked GIC registers with HMP `xp`: QEMU 9.2's current-CPU lookup
can dereference a null CPU outside guest execution. `inspect_rtos_irqs.py`
uses the QOM getter and records actual QEMU exception logs without waking CPUs
or reading the interrupt-acknowledgement register.
Unmodeled MMIO logging retains the first four occurrences of each distinct
CPU/address/size/direction, up to 2048 distinct accesses. The read-only
`/machine` property `unmodeled-mmio-json` preserves full occurrence counts,
last write values and overflow totals, so one busy loop cannot hide later
register discoveries.

Display diagnostic inputs are separate from a hardware display/clock model.
All properties below are on `/machine`:

- `display-test-mode` is boolean and defaults to **false**.
- `display-test-status-mask` is uint32 in 0..3. While test mode is enabled,
  mask bit 0 supplies bit 0 of `0x7c00011c`; mask bit 1 supplies bit 28 of
  `0x7c050ef0`. Other bits retain the original unmodeled zero value. Writes
  from the guest remain ignored. With test mode disabled both registers retain
  their original zero read values, even if a nonzero mask is configured.
- `display-test-pulse` is write-only uint32. Each explicit host write of
  196, 230 or 232 delivers exactly one `qemu_irq_pulse` to that real GIC SPI
  input. Disabled test mode, other IDs and an unrealized GIC are rejected.
  The original GIC configuration still determines routing and delivery.
- `display-test-state-json` is read-only and records the test-only scope,
  enabled state, mask, per-register read counts, typed pulse requests,
  rejected requests and accepted counts for each IRQ.

There is no periodic scheduling, inferred source frequency, automatic success,
guest callback invocation, UI/event/TCB write or DSP-ready fabrication in this
interface. It supports a reversible host-driven diagnostic experiment. The
original consumer/IRQ evidence is `serial/display-clock-evidence.json`; that
evidence does not establish hardware clock/enable/mask semantics.

The independent frame diagnostic interface has `/machine` properties
`frame-test-mode` (boolean, default false), `frame-test-pulse` (write-only
uint32, only absolute GIC IDs 192 and 193), and `frame-test-state-json`
(read-only JSON including `enabled`, `automatic_timing:false`, per-IRQ pulse
counts, requests and rejections). Each accepted explicit host request emits
one real GIC input edge. It supplies no MMIO status or image data and never
writes guest callbacks, messages, UI acknowledgements or DSP state. Disabling
either TEST interface rejects future pulses but does not withdraw an edge
already pending in the GIC. Original interrupt routing/masking remains in
force. Physical frame timing and buffer completion semantics are unverified.
The original 192/193 handlers and five-condition service gate are documented
in `qemu/peer-isa-analysis/protocol-feasibility/postinit-frame-gate-evidence.json`.

In `menu-smp/results/local-timer-rearm`, the original firmware itself advances
the startup sequence index from 1 to 10 and sets its startup-ready byte at
`0x0675948e`. This separately verifies the one-shot LOAD fix with the real
program. The run still waits for later frame-driven services; it is not proof
of menu completion.

The independent `/machine` boolean `tx-serial-hle` defaults to **false**.
When explicitly enabled, the narrow `0x7b193400..0x7b19344b` model accepts
the observed transmit configuration `0x01020002`, `0x300 | byte_count`, the
matching count mask, and 4–32 populated TX bytes beginning with `0x81`.
A START write captures those actual bytes and completes immediately in the
software sink; status `0x7b193444` bit 8 is then set until the original ACK
write `0x10` at `0x7b193430`. The original STOP write ends the transaction.
Unknown START formats, missing bytes, unexpected status reads or lifecycle
operations request a debugger stop without claiming completion.
`tx-serial-state-json` records completed/acknowledged/stopped counts, byte
counts, the first 1024 exact TX packets, drops and unsupported operations.
This HLE creates no RX data, external-device ACK, pixels, DSP execution or
interrupt. Physical peer identity, acceptance and transfer timing are unknown.
With the property off, the original unmodeled MMIO behavior remains in force.

This is an optional controller/performance experiment, **not a necessary
mode35 boot repair**. The original driver has bounded polls and returns an
error after timeout. `menu-smp/results/local-frame-paced-menu-timeout10`
already completed the original mode35 acknowledgement with the device absent
and without synthesizing its completion bit. A short host stage deadline had
interrupted the 157-record batch near its end; it was not an infinite wait.

Validation:

- `0x7c0151f4..0x7c015203` now retains four guest-written 32-bit software
  status words shared by the CPUs. Original `0x06680708`/`0x06680720` write/read
  them, and the loader completion wrapper `0x06469ee8` ORs its own completed
  component mask into word 2. The model starts at zero and supplies no ready
  bit, event, IRQ or scene. The adjacent reset command at `0x7c0151f0` remains
  outside this model. `/machine/software-status-state-json` reports exact
  values and read/write counts. `qemu/software-status-verified/verification.json`
  verifies the original helpers on CPUs 0/1/2 and original `0x06469ebe` mask
  checks: zero fails, writing 2 permits mask 2, overwriting with 8 removes that
  permission, and writing zero clears it again. These tests use synthetic
  source words and do not claim actual component loading or GUI completion.
  The original producer/gate evidence is recorded separately in
  `display/render-wait-analysis/evidence.json` and its disassembly.
- `python3 -B qemu/test_serial_tx_hle.py --output qemu/new-serial-tx-run`
  executes unmodified original transmit functions with synthetic records.
  `qemu/serial-tx-hle-verified/verification.json` records three passing cases:
  default-absent returns original error 1; original `0x067091f2` sends two
  exact 4-byte packets and returns 0 under opt-in HLE; original `0x067093de`
  packs 29 consecutive register writes into a 32-byte transfer. Completion,
  acknowledgement and STOP counts agree. Unknown prefix and incomplete
  byte coverage trigger real debugger stops with no additional completion.
- `python3 -B qemu/test_frame_inputs.py --output qemu/new-frame-test-run`
  verifies the explicit interface using synthetic ARM GIC accesses and no
  camera firmware. `qemu/frame-test-smoke-verified/verification.json` records
  **21/21 PASS**: default disabled, invalid/disabled requests rejected, real
  pending bits for both IRQs, ordinary GIC clear, accounting, unchanged MMIO
  and independence from the display TEST interface.
- `python3 -B qemu/test_ram_layout.py --output qemu/new-ram-layout-run`
  checks both 512-MiB and 2-GiB configurations with synthetic ARM accesses.
  `qemu/results-ram-layout-verified/verification.json` records **83/83 PASS**:
  coherent low/high aliases, distinct backing across the entire high range
  including `0xfd159000`, `0xffa91000`, `0xfffffffc`, no low 512-MiB folding,
  no DRAM at `0x40000000`, and retained module/UART/GPIO/SRAM behavior.
  No camera firmware or allocator runs in this smoke; original boot progress
  must be judged from the separate full-firmware run.
- `python3 -B qemu/test_display_inputs.py --output qemu/new-display-test-run`
  tests the diagnostic interface with no firmware loaded. Synthetic ARM
  LDR/STR/BX helpers configure edge-triggered GIC inputs and CPU0 targets, then
  verify real pending bits from each explicit pulse and normal pending clear.
  `qemu/display-test-smoke-verified/verification.json` records **32/32 PASS**,
  including default-off behavior, independent status bits and byte lanes,
  invalid IRQ/mask rejection and accounting. Earlier retained attempts show
  the host socket sandbox restriction, GDB GIC current-CPU limitation, and a
  test setup missing GIC targets; none is a firmware failure.
- `python3 -B qemu/smoke_smp.py` executes synthetic ARM instructions on all four
  CPUs, verifies MPIDR values and increments one counter through the coherent
  aliases across all CPUs. Result: `qemu/results-smp-smoke/result.json`.
- `python3 -B qemu/smoke_models.py` checks actual guest PL022/CS transactions,
  RTC readback and unsupported-read accounting, module request assert/deassert
  behavior, SRAM storage, all 26 timer countdowns, and each timer's separate
  GIC pending bit on expiry and its withdrawal on interrupt clear. Result:
  `qemu/results-models-smoke/result.json`.
  It also verifies fixed GIC CPU interface aliases, strict versus opt-in SPI0
  behavior, both GPIO access widths, all five UART empty-RX flags, transmitted
  character capture and exact UART interrupt routing.
- Original full-component run `menu-smp/results/full-components-smp-baseline`
  reached the primary barrier with `f0f0f0f0` peer flags, then original CPU1/2
  code cleared those flags and CPU0 reached serial initialization. The run
  explicitly records `peer_flags_manually_cleared=false`. CPU1/2 subsequently
  reached `0x063be8aa`. This is evidence of the multicore startup handshake,
  not evidence of a rendered menu. It used the initial board before the
  stateful RTC/module models; later runs are recorded separately by the runner.
- After the shared SP804 extensions, `qemu/results-linux-regression/result.json`
  confirms the existing `s5-linux` machine still boots original Linux 3.18.18,
  mounts the original SquashFS root and loads ICC/ICSM without kernel panic.
