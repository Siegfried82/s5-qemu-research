/* Research-only boot-state reconstruction; not Panasonic firmware code. */
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
    bx lr
