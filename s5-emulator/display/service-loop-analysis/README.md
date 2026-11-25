# Original task66 service loop experiment

`menu-smp/ui_service_loop.py` enters the original task66 receive/dispatch loop
at `0675efba` while the original DSP initialization remains unfinished. It
requires the stopped task66 context at `065491ee`, Thumb SVC with IRQ enabled,
zero nesting/dispatch guards, a valid original stack with 4 KiB headroom, exact
original loop opcodes and a structurally valid queue76. It reserves 16 bytes
below the suspended stack, writes only SP/R6/PC, and leaves the old live frame
intact. The original code receives messages, processes them and sends fallback
responses. Static messages are permitted; the helper performs no pool release.

`UIServiceLoopProbe(emu, run_postinit=True)` instead enters at `0675efb2`, so
the original `0672060c` and `065b9fcc` calls actually run. Their return points
are observed before the loop begins. It still skips the remainder of the
unfinished DSP initialization. An empty queue blocks in the real RTOS. The
runner must retain an overall deadline. After at most 64 successful receives,
the helper stops at the next iteration boundary after dispatch and any
fallback return, before receiving message 65. Closing removes breakpoints
without restoring the abandoned initialization context.

`evidence.json` and `original.disasm.txt` record the original program hash and
two actual runs: `local-ui-loop` and `local-ui-loop-postinit`. Both received
the existing `40001` and `2b` messages, returned 1 from both original dispatch
calls, and then waited in original receive. Post-init additionally changed
`08aa02af` and `08aa02f4` from 0 to 1 through original instructions. Both still
had four warm-up draws and one swap; neither produced a menu.

The `0672060c -> 0675e6ec` subtree initializes the original ADRKIZU coordinate
correction path, identified by original `vADRKIZU_Stop`,
`lADRKIZU_ExecCorrection` and `lADRKIZU_RenewPosData` strings. Its enable byte
is not evidence of an active UI/frame producer. All six signed coordinate
counts in the inspected snapshot were -1.

`frame-input-contract.json` and `frame-producer.disasm.txt` document queue77:
the original sender uses static 20-byte envelope slots and a shared 0x220-byte
payload at `08c51d9c`, which the consumer copies to `0a603fb8`. The embedded
0x30-byte context is built from mode tables and frame/bank flags, not a proven
pixel-plane ABI. The current payload and context are almost entirely zero.
The next evidence-based route is a configured frame/bank timing interrupt
that lets the original producer populate these objects. It is not valid to
substitute RGB pointers, synthesize queue77 packets or mark mode35 complete.

Host contract checks (not firmware execution):

```sh
PYTHONDONTWRITEBYTECODE=1 python3 s5-emulator/display/test_ui_service_loop.py
```

Seven checks pass: separate frame and register-only writes, empty/static
queues, completion-boundary limits, 64 messages, task migration, actual
post-init return observation, and rejection without repairing live memory.
