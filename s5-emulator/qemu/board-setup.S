/* SPDX-License-Identifier: GPL-2.0-or-later
 * Reconstructed bootloader state, not original Panasonic code.
 * S5's Linux GIC driver expects SPI routing to be configured before entry.
 */
.syntax unified
.arm
.text
.global _start
_start:
    ldr r0, =0x70001000
    mov r1, #1
    str r1, [r0]
    ldr r1, =0x01010101
    add r2, r0, #0x800
    add r2, r2, #0x20
    add r3, r0, #0x800
    add r3, r3, #0x1c0
1:  str r1, [r2], #4
    cmp r2, r3
    bne 1b
    ldr r1, =0xa0a0a0a0
    add r2, r0, #0x400
    add r2, r2, #0x20
    add r3, r0, #0x400
    add r3, r3, #0x1c0
2:  str r1, [r2], #4
    cmp r2, r3
    bne 2b
    dsb sy
    bx lr
.ltorg
