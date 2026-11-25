/* Research-only short-descriptor MMU setup, not Panasonic loader code. */
.syntax unified
.arm
.text
.global _start
_start:
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
    bx lr
