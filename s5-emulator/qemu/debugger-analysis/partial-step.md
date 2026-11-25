# QEMU 9.2.4 TCG partial stepping

This is a read-only audit of the exact local QEMU sources. No QEMU C or binary
change was made for this investigation.

Legacy RSP `s` sets the selected CPU's single-step flag, then calls
`gdb_continue()` and `vm_start()`, which resumes all vCPUs. An instruction
breakpoint shared by those CPUs can therefore stop a different CPU first.
The stop callback clears the single-step flag only on the CPU whose stop is
reported; an interrupted legacy step can also leave another CPU's step flag
pending. This is a debugger-control limitation, not a firmware exception.

Use `vCont;s:<thread-id>` at an all-stop debugger stop to resume only the
selected CPU. `gdb_continue_partial()` leaves unspecified CPUs stopped,
sets the target single-step flag and calls `cpu_resume(target)`. The running
round-robin TCG loop checks `cpu_can_run()` independently for each CPU.

There is a separate **cold-start limitation** in single-threaded TCG:
`rr_cpu_thread_fn()` starts with `while (first_cpu->stopped)`. Resuming only
CPU2 before CPU0 has ever started does not leave that initialization loop,
even though CPU2's `halted` flag is false. The relevant flag is `stopped`.
Once CPU0 has run and this one-time loop has been passed, normal partial
resume/step works without running other CPUs. A synthetic harness can use a
bounded initial warmup before resetting its test registers. Do not insert an
unbounded warmup into a loaded firmware image or treat this as authorization
to run additional guest code unexpectedly.

The original menu experiment has already completed real multicore boot before
its instrumentation steps, so it is past this cold-start condition. Parent
validation in `probe/results/smp-isolated-step-warm/verification.json`
records eight thread-qualified ARM ADD/branch steps across all four CPUs;
each target advances correctly and the other three CPU contexts remain
unchanged, including when their PCs hold real instruction breakpoints.
Tested binary SHA256:
`38fa57d072cadbb7e56a0fb509485167e9aa5b0bd94d280bb634fd2d776d50a9`.

Source anchors, relative to `toolchain/qemu-9.2.4`:

- `gdbstub/gdbstub.c:1348`: legacy single-step calls ordinary continue.
- `gdbstub/system.c:548`: ordinary continue calls `vm_start()`.
- `gdbstub/system.c:559`: per-CPU partial continue.
- `gdbstub/system.c:216`: reported CPU's single-step flag is cleared.
- `system/cpus.c:581`: `cpu_resume()` clears `stop` and `stopped`.
- `system/cpus.c:720`: `vm_prepare_start()` rejects an already-running VM;
  partial commands are intended at a stopped state.
- `accel/tcg/tcg-accel-ops-rr.c:199`: first-CPU-only cold-start gate.
- `accel/tcg/tcg-accel-ops-rr.c:246`: normal loop checks each CPU separately.

The MTTCG worker has no equivalent shared first-CPU initialization gate, but
switching to `thread=multi` was neither required nor tested here and would
change scheduling/concurrency. Keep the current scheduler for reproducibility.
