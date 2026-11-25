/* Minimal Linux ARM EABI PID 1 for the S5 research machine.
 * No libc, serial driver, network, camera service, or external tool is used.
 * /command is executed by the original camera's /bin/sh after chroot.
 */
.syntax unified
.arm
.text
.balign 4
.global _start
.type _start, %function
_start:
    sub sp, sp, #8192
    add r10, sp, #128             /* /command buffer, max 4095 bytes */

    /* The newc archive supplies these too. EEXIST is harmless. */
    ldr r0, =dev_dir
    movw r1, #0755
    mov r7, #39                  /* mkdir */
    svc #0
    ldr r0, =kmsg_path
    movw r1, #020600
    movw r2, #0x010b             /* char 1:11 */
    mov r7, #14                  /* mknod */
    svc #0
    ldr r0, =kmsg_path
    mov r1, #1                   /* O_WRONLY */
    mov r2, #0
    mov r7, #5                   /* open */
    svc #0
    cmp r0, #0
    blt no_kmsg
    mov r6, r0
    mov r1, #1
    mov r7, #63                  /* dup2 */
    svc #0
    mov r0, r6
    mov r1, #2
    svc #0
    ldr r0, =null_path
    mov r1, #0
    mov r7, #5
    svc #0
    cmp r0, #0
    blt 1f
    mov r1, #0
    mov r7, #63
    svc #0
1:
    ldr r0, =begin_message
    bl .Lputs
    ldr r0, =command_path
    mov r1, #0
    mov r7, #5
    svc #0
    cmp r0, #0
    ldrlt r1, =command_error
    blt fatal
    mov r6, r0
    mov r1, r10
    movw r2, #4095
    mov r7, #3                   /* read */
    svc #0
    cmp r0, #0
    ldrle r1, =command_error
    ble fatal
    mov r1, #0
    strb r1, [r10, r0]
    mov r0, r6
    mov r7, #6
    svc #0

    ldr r0, =root_device
    ldr r1, =root_dir
    ldr r2, =squashfs_type
    mov r3, #1                   /* MS_RDONLY */
    mov r4, #0
    mov r7, #21                  /* mount */
    svc #0
    cmp r0, #0
    ldrlt r1, =mount_error
    blt fatal
    ldr r0, =root_ready
    bl .Lputs

    /* Original rootfs2 location; the original /bin tools may link into it. */
    ldr r0, =root2_device
    ldr r1, =root2_dir
    ldr r2, =squashfs_type
    mov r3, #1
    mov r4, #0
    mov r7, #21
    svc #0
    cmp r0, #0
    bge 2f
    ldr r0, =root2_warning
    bl .Lputs
2:
    ldr r0, =proc_type
    ldr r1, =proc_dir
    ldr r2, =proc_type
    mov r3, #0
    mov r4, #0
    mov r7, #21
    svc #0
    cmp r0, #0
    bge 3f
    ldr r0, =proc_warning
    bl .Lputs
3:
    add r0, sp, #4               /* pipe descriptors at sp+4 and sp+8 */
    mov r7, #42                  /* pipe */
    svc #0
    cmp r0, #0
    ldrlt r1, =pipe_error
    blt fatal
    mov r7, #2                   /* fork */
    svc #0
    cmp r0, #0
    ldrlt r1, =fork_error
    blt fatal
    beq child
    mov r6, r0
    ldr r0, [sp, #8]             /* parent closes pipe write end */
    mov r7, #6
    svc #0
    add r9, sp, #4608            /* encoded record buffer, separate from command */
read_output:
    ldr r0, [sp, #4]
    mov r1, r10
    mov r2, #384                 /* <= 768 hex bytes + prefix, below kmsg limit */
    mov r7, #3
    svc #0
    cmn r0, #4
    beq read_output
    cmp r0, #0
    ldrlt r1, =read_error
    blt fatal
    beq output_eof
    mov r11, r0
    ldr r1, =output_prefix
    mov r2, #0
7:
    ldrb r3, [r1, r2]
    cmp r3, #0
    strbne r3, [r9, r2]
    addne r2, r2, #1
    bne 7b
    add r4, r9, r2
    mov r5, #0
    ldr r12, =hex_digits
8:
    ldrb r3, [r10, r5]
    lsr r2, r3, #4
    and r3, r3, #15
    ldrb r2, [r12, r2]
    ldrb r3, [r12, r3]
    strb r2, [r4], #1
    strb r3, [r4], #1
    add r5, r5, #1
    cmp r5, r11
    blt 8b
    mov r3, #10
    strb r3, [r4], #1
    mov r3, #0
    strb r3, [r4]
    mov r0, r9
    bl .Lputs
    cmp r0, #0
    ldrlt r1, =write_error
    blt fatal
    b read_output
output_eof:
    ldr r0, [sp, #4]
    mov r7, #6
    svc #0
wait_child:
    mov r0, r6
    mov r1, sp                   /* int wait status */
    mov r2, #0
    mov r3, #0
    mov r7, #114                 /* wait4 */
    svc #0
    cmn r0, #4                   /* EINTR */
    beq wait_child
    cmp r0, #0
    ldrlt r1, =wait_error
    blt fatal
    ldr r8, [sp]
    bl .Lprint_status
idle:
    mov r7, #29                  /* pause: runner stops QEMU after END */
    svc #0
    b idle

child:
    ldr r0, [sp, #4]             /* child closes pipe read end */
    mov r7, #6
    svc #0
    ldr r0, [sp, #8]
    mov r1, #1
    mov r7, #63
    svc #0
    ldr r0, [sp, #8]
    mov r1, #2
    svc #0
    ldr r0, [sp, #8]
    mov r7, #6
    svc #0
    ldr r0, =root_dir
    mov r7, #61                  /* chroot */
    svc #0
    cmp r0, #0
    ldrlt r1, =chroot_error
    blt child_fatal
    ldr r0, =slash
    mov r7, #12                  /* chdir */
    svc #0
    cmp r0, #0
    ldrlt r1, =chdir_error
    blt child_fatal
    ldr r0, =shell_path
    ldr r3, =shell_option
    str r0, [sp, #16]
    str r3, [sp, #20]
    str r10, [sp, #24]
    mov r3, #0
    str r3, [sp, #28]
    ldr r4, =path_env
    str r4, [sp, #32]
    ldr r4, =home_env
    str r4, [sp, #36]
    ldr r4, =locale_env
    str r4, [sp, #40]
    str r3, [sp, #44]
    add r1, sp, #16
    add r2, sp, #32
    mov r7, #11                  /* execve */
    svc #0
    ldr r1, =exec_error
child_fatal:
    mov r0, r1
    bl .Lputs
    mov r0, #127
    mov r7, #1
    svc #0
    b idle

fatal:
    mov r8, r0                   /* raw negative errno, not wait status */
    mov r0, r1
    bl .Lputs
    bl .Lprint_status
    b idle
no_kmsg:
    mov r0, #126
    mov r7, #1
    svc #0
    b idle

/* Write one NUL-terminated message as one kernel log record. */
.Lputs:
    push {r4, r7, lr}
    mov r1, r0
    mov r2, #0
4:
    ldrb r3, [r1, r2]
    cmp r3, #0
    addne r2, r2, #1
    bne 4b
    mov r0, #1
    mov r7, #4
    svc #0
    pop {r4, r7, pc}

/* The END value is raw wait4 status; 00000000 means exit(0). */
.Lprint_status:
    push {r4, r5, r6, r7, lr}
    sub sp, sp, #64
    ldr r1, =status_template
    mov r2, #0
5:
    ldrb r3, [r1, r2]
    strb r3, [sp, r2]
    cmp r3, #0
    addne r2, r2, #1
    bne 5b
    add r4, sp, #(status_digits-status_template)
    mov r5, #28
    ldr r6, =hex_digits
6:
    lsr r3, r8, r5
    and r3, r3, #15
    ldrb r3, [r6, r3]
    strb r3, [r4], #1
    subs r5, r5, #4
    bge 6b
    mov r0, sp
    bl .Lputs
    add sp, sp, #64
    pop {r4, r5, r6, r7, pc}

.balign 4
.balign 4
dev_dir:       .asciz "/dev"
.balign 4
kmsg_path:     .asciz "/dev/kmsg"
.balign 4
null_path:     .asciz "/dev/null"
.balign 4
command_path:  .asciz "/command"
.balign 4
root_device:   .asciz "/dev/mtdblock0"
.balign 4
root2_device:  .asciz "/dev/mtdblock1"
.balign 4
root_dir:      .asciz "/root"
.balign 4
root2_dir:     .asciz "/root/mnt/rfs2"
.balign 4
proc_dir:      .asciz "/root/proc"
.balign 4
squashfs_type: .asciz "squashfs"
.balign 4
proc_type:     .asciz "proc"
.balign 4
slash:         .asciz "/"
.balign 4
shell_path:    .asciz "/bin/sh"
.balign 4
shell_option:  .asciz "-c"
.balign 4
path_env:      .asciz "PATH=/bin:/sbin:/usr/bin:/usr/sbin:/mnt/rfs2/bin:/mnt/rfs2/usr/bin"
.balign 4
home_env:      .asciz "HOME=/root"
.balign 4
locale_env:    .asciz "LC_ALL=C"
.balign 4
begin_message:.asciz "S5LAB_BEGIN\n"
.balign 4
root_ready:   .asciz "S5LAB_ROOTFS_MOUNTED\n"
.balign 4
root2_warning:.asciz "S5LAB_WARNING rootfs2 mount failed\n"
.balign 4
proc_warning: .asciz "S5LAB_WARNING proc mount failed\n"
.balign 4
command_error:.asciz "S5LAB_ERROR reading /command\n"
.balign 4
mount_error:  .asciz "S5LAB_ERROR mounting original rootfs1\n"
.balign 4
fork_error:   .asciz "S5LAB_ERROR fork\n"
.balign 4
pipe_error:   .asciz "S5LAB_ERROR pipe\n"
.balign 4
read_error:   .asciz "S5LAB_ERROR reading command output pipe\n"
.balign 4
write_error:  .asciz "S5LAB_ERROR writing encoded command output\n"
.balign 4
wait_error:   .asciz "S5LAB_ERROR wait4\n"
.balign 4
chroot_error: .asciz "S5LAB_ERROR chroot\n"
.balign 4
chdir_error:  .asciz "S5LAB_ERROR chdir\n"
.balign 4
exec_error:   .asciz "S5LAB_ERROR execve original /bin/sh\n"
.balign 4
status_template: .ascii "S5LAB_END status=0x"
status_digits:   .asciz "00000000\n"
.balign 4
output_prefix:   .asciz "S5LAB_OUT "
.balign 4
hex_digits:      .ascii "0123456789abcdef"
.balign 4
