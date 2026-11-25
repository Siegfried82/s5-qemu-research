/* Explicit loader-state reconstruction, not firmware. All RTOS cores use it. */
.syntax unified
.arm
.fpu neon
.text
.global _start
_start:
    mrc p15, 0, r0, c1, c0, 2
    orr r0, r0, #0x00f00000
    mcr p15, 0, r0, c1, c0, 2
    isb
    mov r0, #0x40000000
    vmsr fpexc, r0
    mov r0, #0
    mcr p15, 0, r0, c2, c0, 2
    mov r0, #0x00100000
    mcr p15, 0, r0, c2, c0, 0
    mov r0, #1
    mcr p15, 0, r0, c3, c0, 0
    mov r0, #0
    mcr p15, 0, r0, c8, c7, 0
    dsb sy
    mrc p15, 0, r0, c1, c0, 0
    orr r0, r0, #1
    mcr p15, 0, r0, c1, c0, 0
    isb
    /* Loader2 0x5e6 (also loader3 0x2062) leaves timer1 running:
       ENABLE | 32BIT | DIV16. Reproduce its exact writes on CPU0 only. */
    mrc p15, 0, r0, c0, c0, 5
    ands r0, r0, #3
    bne 1f
    /* Execute the original loader3 table-driven GIC routing function. */
    mov sp, #0x13000
    ldr r0, =0x10e9
    blx r0
    ldr r1, =0x70001000
    mov r0, #1
    str r0, [r1]
    ldr r1, =0x7f020028
    mov r0, #0
    str r0, [r1]
    mov r0, #6
    str r0, [r1]
    mov r0, #0x86
    str r0, [r1]
1:
    /* Original loader3 0x1a0..0x1d0 uses physical per-core GICC aliases. */
    mrc p15, 0, r0, c0, c0, 5
    and r0, r0, #3
    ldr r1, =0x70002000
    add r1, r1, r0, lsl #15
    mov r0, #1
    str r0, [r1]
    mov r0, #0xf0
    str r0, [r1, #4]
    mov r0, #0x800
    movw r1, #0x187d
    movt r1, #0xc5
    mov r2, #3
    mvn r3, #0
    mov sp, #0x13000
    mov lr, #0x3c4
    ldr pc, =0x06280004

    .ltorg
    /* Optional Linux CPU3. The original loader writes its own sentinel,
       waits for the actual RTOS SGI2/mailbox release and executes zboot.
       Leave MMU off as at reset; zboot/kernel establish their own state. */
    .org 0x400
linux_cpu3_loader:
    ldr r1, =0x7001a000
    mov r0, #1
    str r0, [r1]
    mov r0, #0xf0
    str r0, [r1, #4]
    mov r0, #0x330
    bx r0
    .ltorg
