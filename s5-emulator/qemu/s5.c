/*
 * Panasonic S5 Linux research machine (firmware V2.9).
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This models the Linux-side CPU and a small, evidence-backed peripheral set.
 * It does not model the camera's other cores, imaging pipeline or boot ROM.
 * The clock/IPC backing regions below are deliberate research stubs.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "qemu/units.h"
#include "exec/address-spaces.h"
#include "hw/boards.h"
#include "hw/sysbus.h"
#include "hw/arm/boot.h"
#include "hw/char/pl011.h"
#include "hw/intc/arm_gic.h"
#include "hw/qdev-properties.h"
#include "hw/loader.h"
#include "sysemu/sysemu.h"
#include "target/arm/cpu-qom.h"
#include "board-setup.h"

typedef struct S5MachineState {
    MachineState parent_obj;
    struct arm_boot_info boot;
    MemoryRegion clock_stub;
    MemoryRegion ipc_stub;
    MemoryRegion unknown_mmio;
    unsigned unknown_count;
} S5MachineState;

#define TYPE_S5_MACHINE MACHINE_TYPE_NAME("s5-linux")
OBJECT_DECLARE_SIMPLE_TYPE(S5MachineState, S5_MACHINE)

static uint64_t s5_unknown_read(void *opaque, hwaddr offset, unsigned size)
{
    S5MachineState *s = opaque;
    if (s->unknown_count++ < 256) {
        qemu_log_mask(LOG_UNIMP,
                      "S5_UNMODELED read addr=0x%08" HWADDR_PRIx
                      " size=%u result=0\n", offset + 0x70000000, size);
    }
    return 0;
}

static void s5_unknown_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    S5MachineState *s = opaque;
    if (s->unknown_count++ < 256) {
        qemu_log_mask(LOG_UNIMP,
                      "S5_UNMODELED write addr=0x%08" HWADDR_PRIx
                      " size=%u value=0x%" PRIx64 " ignored\n",
                      offset + 0x70000000, size, value);
    }
}

static const MemoryRegionOps s5_unknown_ops = {
    .read = s5_unknown_read,
    .write = s5_unknown_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void s5_write_board_setup(ARMCPU *cpu, const struct arm_boot_info *info)
{
    rom_add_blob_fixed("s5.reconstructed-gic-setup", s5_board_setup,
                       sizeof(s5_board_setup), info->board_setup_addr);
}

static void s5_init(MachineState *machine)
{
    S5MachineState *s = S5_MACHINE(machine);
    MemoryRegion *sysmem = get_system_memory();
    Object *cpuobj = object_new(machine->cpu_type);
    DeviceState *gic, *timer;
    SysBusDevice *gicbus, *timerbus;

    object_property_set_bool(cpuobj, "has_el3", false, &error_fatal);
    object_property_set_bool(cpuobj, "has_el2", false, &error_fatal);
    object_property_set_int(cpuobj, "reset-cbar", 0x70000000, &error_fatal);
    qdev_realize(DEVICE(cpuobj), NULL, &error_fatal);

    /* 64 MiB covers Linux RAM and both memory-backed SquashFS images. */
    memory_region_add_subregion(sysmem, 0x80000000, machine->ram);
    memory_region_init_io(&s->unknown_mmio, OBJECT(machine), &s5_unknown_ops,
                          s, "s5.unmodeled-mmio", 0x10000000);
    memory_region_add_subregion_overlap(sysmem, 0x70000000,
                                        &s->unknown_mmio, -100);

    /* pvc_map_io()/pvc_init_early(): readable/writable research substitutes. */
    memory_region_init_ram_nomigrate(&s->clock_stub, OBJECT(machine),
                           "s5.clock-register-stub", 0x1000, &error_fatal);
    memory_region_add_subregion(sysmem, 0x7f038000, &s->clock_stub);
    memory_region_init_ram_nomigrate(&s->ipc_stub, OBJECT(machine),
                           "s5.ipc-register-stub", 0x2000, &error_fatal);
    memory_region_add_subregion(sysmem, 0x00060000, &s->ipc_stub);

    /* The original SMP kernel reads banked ITARGETSR to discover its CPU mask.
     * QEMU's uniprocessor GIC makes these registers RAZ/WI.  Keep four GIC
     * interfaces while only CPU0 executes; no other camera cores are modeled.
     * 448 matches the firmware's irq descriptor bound.
     */
    gic = qdev_new(TYPE_ARM_GIC);
    qdev_prop_set_uint32(gic, "revision", 2);
    qdev_prop_set_uint32(gic, "num-cpu", 4);
    qdev_prop_set_uint32(gic, "num-irq", 448);
    qdev_prop_set_bit(gic, "has-security-extensions", false);
    qdev_prop_set_bit(gic, "has-virtualization-extensions", false);
    gicbus = SYS_BUS_DEVICE(gic);
    sysbus_realize_and_unref(gicbus, &error_fatal);
    sysbus_mmio_map(gicbus, 0, 0x70001000);
    sysbus_mmio_map(gicbus, 1, 0x70002000);
    sysbus_connect_irq(gicbus, 0, qdev_get_gpio_in(DEVICE(cpuobj), ARM_CPU_IRQ));
    sysbus_connect_irq(gicbus, 4, qdev_get_gpio_in(DEVICE(cpuobj), ARM_CPU_FIQ));
    sysbus_connect_irq(gicbus, 8, qdev_get_gpio_in(DEVICE(cpuobj), ARM_CPU_VIRQ));
    sysbus_connect_irq(gicbus, 12, qdev_get_gpio_in(DEVICE(cpuobj), ARM_CPU_VFIQ));

    pl011_create(0x7f050000, qdev_get_gpio_in(gic, 53), serial_hd(0));
    timer = qdev_new("sp804");
    qdev_prop_set_uint32(timer, "freq0", 27000000);
    qdev_prop_set_uint32(timer, "freq1", 27000000);
    timerbus = SYS_BUS_DEVICE(timer);
    sysbus_realize_and_unref(timerbus, &error_fatal);
    sysbus_mmio_map(timerbus, 0, 0x7f02a000);
    sysbus_connect_irq(timerbus, 0, qdev_get_gpio_in(gic, 24));

    s->boot.ram_size = 0x02f00000;
    s->boot.loader_start = 0x80200000;
    s->boot.board_id = -1;
    s->boot.gic_cpu_if_addr = 0x70002000;
    s->boot.board_setup_addr = 0x80010000;
    s->boot.write_board_setup = s5_write_board_setup;
    arm_load_kernel(ARM_CPU(cpuobj), machine, &s->boot);
}

static void s5_machine_class_init(ObjectClass *oc, void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    mc->desc = "Panasonic S5 Linux research machine (partial, V2.9)";
    mc->init = s5_init;
    mc->max_cpus = 1;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-a7");
    mc->default_ram_size = 64 * MiB;
    mc->default_ram_id = "s5.ram";
    mc->ignore_memory_transaction_failures = false;
}

static const TypeInfo s5_machine_type = {
    .name = TYPE_S5_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(S5MachineState),
    .class_init = s5_machine_class_init,
};

static void s5_machine_register_types(void)
{
    type_register_static(&s5_machine_type);
}

type_init(s5_machine_register_types)
