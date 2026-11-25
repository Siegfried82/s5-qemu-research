/*
 * Panasonic S5 RTOS SMP research machine (firmware V2.9).
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This is a partial hardware model.  Four real Cortex-A7 CPU instances share
 * the same RAM through low and high aliases.  Firmware, loader CPU state and
 * peripheral peers must be supplied by the experiment; this board does not
 * clear firmware handshake flags, patch code or synthesize a menu.
 */
#include "qemu/osdep.h"
#include "qemu/cutils.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "qemu/error-report.h"
#include "qemu/log.h"
#include "qemu/units.h"
#include "exec/address-spaces.h"
#include "hw/boards.h"
#include "hw/sysbus.h"
#include "hw/irq.h"
#include "hw/char/pl011.h"
#include "hw/intc/arm_gic.h"
#include "hw/qdev-properties.h"
#include "hw/ssi/pl022.h"
#include "hw/ssi/ssi.h"
#include "chardev/char.h"
#include "sysemu/sysemu.h"
#include "target/arm/cpu.h"

#define TYPE_S5_RTOS_MACHINE MACHINE_TYPE_NAME("s5-rtos")
#define S5_RTOS_CPUS 4
#define S5_RTOS_IRQS 448
OBJECT_DECLARE_SIMPLE_TYPE(S5RTOSMachineState, S5_RTOS_MACHINE)

typedef struct S5GPIOInput {
    MemoryRegion iomem;
    S5RTOSMachineState *machine;
    hwaddr base;
} S5GPIOInput;

typedef struct S5UnknownAccess {
    hwaddr address;
    uint64_t count, last_value;
    unsigned size;
    int cpu;
    bool write;
} S5UnknownAccess;

typedef struct S5ADC {
    MemoryRegion iomem;
    S5RTOSMachineState *machine;
    hwaddr base;
    unsigned index, channels;
    uint32_t control, scan, count, mask, status;
    uint16_t results[12];
    uint64_t conversions, unknown_reads, unknown_writes;
} S5ADC;

typedef struct S5I2C {
    MemoryRegion iomem;
    S5RTOSMachineState *machine;
    qemu_irq irq;
    hwaddr base;
    unsigned index, gic_id;
    uint32_t regs[5];
    bool irq_level;
    uint64_t starts, errors, irq_assertions, irq_clears;
    uint64_t unknown_reads, unknown_writes;
} S5I2C;

typedef struct S5SPIProbe {
    MemoryRegion data_gate;
    MemoryRegion *controller_region;
    unsigned index;
    uint32_t cs_value;
    uint64_t cs_writes, data_accesses, open_bus_reads, strict_stops;
    uint32_t last_data, last_pc;
    unsigned last_size;
    int last_cpu;
    bool last_write, open_bus;
} S5SPIProbe;

typedef struct S5SerialTX {
    bool enabled;
    uint32_t regs[0x4c / 4], written_words, stage;
    uint64_t starts, completed, acknowledgements, stops, unsupported;
    uint64_t bytes, dropped;
    unsigned records;
    char packets[1024][65];
    char last_error[96];
} S5SerialTX;

struct S5RTOSMachineState {
    MachineState parent_obj;
    MemoryRegion low_alias;
    MemoryRegion high_alias;
    MemoryRegion gic_dist_alias[3];
    MemoryRegion unknown_mmio;
    MemoryRegion spi_cs;
    MemoryRegion module_controls;
    MemoryRegion software_status;
    MemoryRegion pool_sram;
    MemoryRegion coprocessor_sram;
    MemoryRegion spi0_cs_capture;
    S5SPIProbe spi_probes[7];
    DeviceState *rtc_peer;
    DeviceState *gic;
    S5GPIOInput startup_inputs[2];
    S5ADC adc[3];
    uint32_t adc11_sample;
    S5I2C i2c[5];
    bool i2c_generic_error;
    bool inactive_inputs;
    uint32_t spi_cs_value;
    uint32_t module_regs[0x1000 / 4];
    uint64_t module_requests;
    uint64_t module_unknown_reads;
    uint64_t module_unknown_writes;
    uint32_t software_status_words[4];
    uint64_t software_status_reads[4], software_status_writes[4];
    GHashTable *unknown_index;
    S5UnknownAccess unknown_accesses[2048];
    unsigned unknown_unique;
    uint64_t unknown_total, unknown_dropped;
    bool display_test_mode;
    uint32_t display_test_status_mask;
    uint32_t display_test_last_requested_irq;
    uint64_t display_test_reads[2];
    uint64_t display_test_pulse_requests, display_test_pulse_rejected;
    uint64_t display_test_pulses[3];
    bool frame_test_mode;
    uint32_t frame_test_last_requested_irq;
    uint64_t frame_test_pulse_requests, frame_test_pulse_rejected;
    uint64_t frame_test_pulses[2];
    S5SerialTX serial_tx;
};

/* Original 0x06680708/0x06680720 copy exactly four words to/from this bank.
 * The loader completion wrapper 0x06469ee8 performs a locked software OR in
 * word 2, and 0x06469ebe tests the same word. Retain only actual guest writes:
 * this is not a hardware-ready generator and emits no IRQ/event/message.
 * The adjacent 0x7c0151f0 reset/toggle command is deliberately outside it.
 */
static uint64_t s5_rtos_software_status_read(void *opaque, hwaddr offset,
                                          unsigned size)
{
    S5RTOSMachineState *s = opaque;
    unsigned i = offset / 4;
    s->software_status_reads[i]++;
    return s->software_status_words[i];
}

static void s5_rtos_software_status_write(void *opaque, hwaddr offset,
                                        uint64_t value, unsigned size)
{
    S5RTOSMachineState *s = opaque;
    unsigned i = offset / 4;
    s->software_status_writes[i]++;
    s->software_status_words[i] = value;
}

static const MemoryRegionOps s5_rtos_software_status_ops = {
    .read = s5_rtos_software_status_read,
    .write = s5_rtos_software_status_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
    .impl.min_access_size = 4,
    .impl.max_access_size = 4,
};

static char *s5_rtos_software_status_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new("{\"scope\":\"four guest-owned software status words; no automatic ready bits\","
        "\"base\":\"0x7c0151f4\",\"initial_value\":0,"
        "\"automatic_completion\":false,\"irq_or_event_generated\":false,"
        "\"adjacent_reset_command_modeled\":false,\"registers\":[");
    unsigned i;
    for (i = 0; i < 4; i++) {
        g_string_append_printf(json, "%s{\"address\":\"0x%08x\","
            "\"value\":%u,\"reads\":%" PRIu64 ",\"writes\":%" PRIu64 "}",
            i ? "," : "", 0x7c0151f4 + 4 * i, s->software_status_words[i],
            s->software_status_reads[i], s->software_status_writes[i]);
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

static void s5_serial_tx_unsupported(S5SerialTX *tx, const char *reason)
{
    tx->unsupported++;
    pstrcpy(tx->last_error, sizeof(tx->last_error), reason);
    qemu_log_mask(LOG_UNIMP, "S5_SERIAL_TX_HLE unsupported=%s\n", reason);
    if (current_cpu) {
        cpu_interrupt(current_cpu, CPU_INTERRUPT_DEBUG);
    }
}

static uint64_t s5_serial_tx_read(S5SerialTX *tx, hwaddr offset, unsigned size)
{
    if (size != 4 || offset != 0x44) {
        s5_serial_tx_unsupported(tx, "read outside known 32-bit completion status");
        return 0;
    }
    return tx->stage == 1 ? 0x100 : 0;
}

static void s5_serial_tx_write(S5SerialTX *tx, hwaddr offset,
                               uint64_t value, unsigned size)
{
    unsigned count, words, i;
    uint32_t mask;
    char packet[65];
    if (size != 4 || (offset & 3)) {
        s5_serial_tx_unsupported(tx, "unsupported write size or alignment");
        return;
    }
    if (offset >= 0x10 && offset < 0x30) {
        if (tx->stage) {
            s5_serial_tx_unsupported(tx, "TX buffer changed during active transfer");
            return;
        }
        tx->regs[offset / 4] = value;
        tx->written_words |= 1U << ((offset - 0x10) / 4);
        return;
    }
    switch (offset) {
    case 0x00: /* exact observed configuration, validated at START */
    case 0x04: /* upper config + byte count */
    case 0x0c: /* count-derived byte mask */
    case 0x3c: /* observed startup/config writes; no invented side effect */
    case 0x48:
        tx->regs[offset / 4] = value;
        return;
    case 0x30:
        if (value == 0) {
            tx->regs[offset / 4] = 0;
            return;
        }
        if (value == 0x10) {
            if (tx->stage == 1) {
                tx->acknowledgements++;
                tx->stage = 2;
            }
            tx->regs[offset / 4] = value;
            return;
        }
        s5_serial_tx_unsupported(tx, "unknown completion acknowledgement");
        return;
    case 0x08:
        if (value == 0) {
            if (tx->stage == 1) {
                s5_serial_tx_unsupported(tx, "STOP before completion acknowledgement");
                return;
            }
            if (tx->stage == 2) {
                tx->stops++;
            }
            tx->stage = 0;
            tx->written_words = 0;
            tx->regs[offset / 4] = 0;
            return;
        }
        tx->starts++;
        count = tx->regs[1] & 0x3f;
        words = (count + 3) / 4;
        mask = count == 32 ? 0 : UINT32_MAX << (count & 31);
        if (value != 1 || tx->stage || tx->regs[0] != 0x01020002 ||
            (tx->regs[1] & ~0x3fU) != 0x300 || count < 4 || count > 32 ||
            tx->regs[3] != mask || tx->regs[0x48 / 4] != 1 ||
            (tx->written_words & ((1U << words) - 1)) != (1U << words) - 1 ||
            (tx->regs[4] & 0xff) != 0x81) {
            s5_serial_tx_unsupported(tx, "unknown START/config/count/mask/TX prefix or missing bytes");
            return;
        }
        for (i = 0; i < count; i++) {
            unsigned byte = (tx->regs[4 + i / 4] >> ((i & 3) * 8)) & 0xff;
            snprintf(packet + i * 2, 3, "%02x", byte);
        }
        if (tx->records < ARRAY_SIZE(tx->packets)) {
            pstrcpy(tx->packets[tx->records++], sizeof(tx->packets[0]), packet);
        } else {
            tx->dropped++;
        }
        tx->bytes += count;
        tx->completed++;
        tx->stage = 1;
        tx->regs[offset / 4] = 1;
        qemu_log_mask(LOG_UNIMP, "S5_SERIAL_TX_HLE sink-complete bytes=%u tx=%s "
                      "peer-acceptance-unverified\n", count, packet);
        return;
    default:
        s5_serial_tx_unsupported(tx, "write outside known transmit lifecycle");
        return;
    }
}

static bool s5_rtos_get_serial_tx_hle(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->serial_tx.enabled;
}

static void s5_rtos_set_serial_tx_hle(Object *obj, bool value, Error **errp)
{
    S5SerialTX *tx = &S5_RTOS_MACHINE(obj)->serial_tx;
    if (tx->stage) {
        error_setg(errp, "tx-serial-hle cannot change during an active transfer");
        return;
    }
    tx->enabled = value;
}

static char *s5_rtos_serial_tx_state(Object *obj, Error **errp)
{
    S5SerialTX *tx = &S5_RTOS_MACHINE(obj)->serial_tx;
    GString *json = g_string_new(NULL);
    unsigned i;
    g_string_append_printf(json,
        "{\"scope\":\"opt-in TX-only controller sink HLE\",\"enabled\":%s,"
        "\"physical_peer_acceptance_verified\":false,\"rx_or_peer_ack_generated\":false,"
        "\"pixel_or_dsp_execution_generated\":false,\"timing_verified\":false,"
        "\"stage\":%u,\"starts\":%" PRIu64 ",\"completed\":%" PRIu64
        ",\"acknowledgements\":%" PRIu64 ",\"stops\":%" PRIu64
        ",\"bytes\":%" PRIu64 ",\"unsupported\":%" PRIu64
        ",\"last_error\":\"%s\",\"records_dropped\":%" PRIu64 ",\"tx_packets_hex\":[",
        tx->enabled ? "true" : "false", tx->stage, tx->starts, tx->completed,
        tx->acknowledgements, tx->stops, tx->bytes, tx->unsupported,
        tx->last_error, tx->dropped);
    for (i = 0; i < tx->records; i++) {
        g_string_append_printf(json, "%s\"%s\"", i ? "," : "", tx->packets[i]);
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

/* Explicit host-driven diagnostic inputs, not a display clock/device model.
 * No timer, guest callback, firmware flag, task or peer mailbox is modified.
 */
static const unsigned s5_display_test_irqs[] = { 196, 230, 232 };

static bool s5_rtos_get_display_test_mode(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->display_test_mode;
}

static void s5_rtos_set_display_test_mode(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->display_test_mode = value;
}

static void s5_rtos_get_display_test_mask(Object *obj, Visitor *v,
                                         const char *name, void *opaque,
                                         Error **errp)
{
    uint32_t value = S5_RTOS_MACHINE(obj)->display_test_status_mask;
    visit_type_uint32(v, name, &value, errp);
}

static void s5_rtos_set_display_test_mask(Object *obj, Visitor *v,
                                         const char *name, void *opaque,
                                         Error **errp)
{
    uint32_t value;
    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    if (value > 3) {
        error_setg(errp, "display-test-status-mask must be in 0..3");
        return;
    }
    S5_RTOS_MACHINE(obj)->display_test_status_mask = value;
}

static void s5_rtos_set_display_test_pulse(Object *obj, Visitor *v,
                                          const char *name, void *opaque,
                                          Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    uint32_t irq;
    unsigned i;
    if (!visit_type_uint32(v, name, &irq, errp)) {
        return;
    }
    s->display_test_pulse_requests++;
    s->display_test_last_requested_irq = irq;
    for (i = 0; i < ARRAY_SIZE(s5_display_test_irqs); i++) {
        if (irq == s5_display_test_irqs[i]) {
            break;
        }
    }
    if (!s->display_test_mode || !s->gic ||
        i == ARRAY_SIZE(s5_display_test_irqs)) {
        s->display_test_pulse_rejected++;
        error_setg(errp, "display-test-pulse requires display-test-mode=on, "
                   "a realized GIC and IRQ 196, 230 or 232");
        return;
    }
    s->display_test_pulses[i]++;
    qemu_log_mask(LOG_UNIMP, "S5_DISPLAY_TEST host-requested-pulse irq=%u "
                  "count=%" PRIu64 " physical-timing-unverified\n",
                  irq, s->display_test_pulses[i]);
    qemu_irq_pulse(qdev_get_gpio_in(s->gic, irq - GIC_INTERNAL));
}

static char *s5_rtos_display_test_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    return g_strdup_printf(
        "{\"scope\":\"test-only explicit host status and IRQ inputs\","
        "\"automatic_timing\":false,\"hardware_clock_verified\":false,"
        "\"guest_callback_or_flag_writes\":false,\"enabled\":%s,"
        "\"status_mask\":%u,\"reads\":{\"0x7c00011c\":%" PRIu64
        ",\"0x7c050ef0\":%" PRIu64 "},\"pulse_requests\":%" PRIu64
        ",\"pulse_rejected\":%" PRIu64 ",\"last_requested_irq\":%u,"
        "\"pulses\":{\"196\":%" PRIu64 ",\"230\":%" PRIu64
        ",\"232\":%" PRIu64 "}}",
        s->display_test_mode ? "true" : "false",
        s->display_test_status_mask, s->display_test_reads[0],
        s->display_test_reads[1], s->display_test_pulse_requests,
        s->display_test_pulse_rejected, s->display_test_last_requested_irq,
        s->display_test_pulses[0], s->display_test_pulses[1],
        s->display_test_pulses[2]);
}

/* Bounded external TEST edges only. No frame buffers, MMIO status, DSP state,
 * callbacks, RTOS messages or UI acknowledgements are fabricated here. */
static bool s5_rtos_get_frame_test_mode(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->frame_test_mode;
}

static void s5_rtos_set_frame_test_mode(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->frame_test_mode = value;
}

static void s5_rtos_set_frame_test_pulse(Object *obj, Visitor *v,
                                       const char *name, void *opaque,
                                       Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    uint32_t irq;
    if (!visit_type_uint32(v, name, &irq, errp)) {
        return;
    }
    s->frame_test_pulse_requests++;
    s->frame_test_last_requested_irq = irq;
    if (!s->frame_test_mode || !s->gic || (irq != 192 && irq != 193)) {
        s->frame_test_pulse_rejected++;
        error_setg(errp, "frame-test-pulse requires frame-test-mode=on, "
                   "a realized GIC and IRQ 192 or 193");
        return;
    }
    s->frame_test_pulses[irq - 192]++;
    qemu_log_mask(LOG_UNIMP, "S5_FRAME_TEST host-requested-pulse irq=%u "
                  "count=%" PRIu64 " physical-timing-unverified\n",
                  irq, s->frame_test_pulses[irq - 192]);
    qemu_irq_pulse(qdev_get_gpio_in(s->gic, irq - GIC_INTERNAL));
}

static char *s5_rtos_frame_test_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    return g_strdup_printf(
        "{\"scope\":\"test-only explicit host IRQ inputs; no frame data\","
        "\"automatic_timing\":false,\"hardware_clock_verified\":false,"
        "\"guest_callback_or_flag_writes\":false,\"mmio_status_modified\":false,"
        "\"enabled\":%s,\"pulse_requests\":%" PRIu64
        ",\"pulse_rejected\":%" PRIu64 ",\"last_requested_irq\":%u,"
        "\"pulses\":{\"192\":%" PRIu64 ",\"193\":%" PRIu64 "}}",
        s->frame_test_mode ? "true" : "false", s->frame_test_pulse_requests,
        s->frame_test_pulse_rejected, s->frame_test_last_requested_irq,
        s->frame_test_pulses[0], s->frame_test_pulses[1]);
}

static char *s5_rtos_ram_layout(Object *obj, Error **errp)
{
    MachineState *machine = MACHINE(obj);
    return g_strdup_printf(
        "{\"scope\":\"experimental firmware address-space backing\","
        "\"physical_camera_capacity_verified\":false,\"backing_bytes\":%"
        PRIu64 ",\"low_alias\":{\"base\":0,\"size\":%" PRIu64
        "},\"high_alias\":{\"base\":2147483648,\"size\":%" PRIu64
        "},\"low_0x40000000_to_0x7fffffff_is_dram\":false}",
        (uint64_t)machine->ram_size,
        (uint64_t)MIN(machine->ram_size, 1 * GiB),
        (uint64_t)machine->ram_size);
}

static void s5_rtos_i2c_update(S5I2C *bus)
{
    bool level = (bus->regs[0] & 0x30) == 0x30;
    if (level != bus->irq_level) {
        if (level) {
            bus->irq_assertions++;
        } else {
            bus->irq_clears++;
        }
        bus->irq_level = level;
    }
    qemu_set_irq(bus->irq, level);
}

static uint64_t s5_rtos_i2c_read(void *opaque, hwaddr offset, unsigned size)
{
    S5I2C *bus = opaque;
    if (!(offset & 3) && offset <= 0x10) {
        return bus->regs[offset / 4];
    }
    bus->unknown_reads++;
    qemu_log_mask(LOG_UNIMP, "S5_I2C_UNSUPPORTED bus=%u read offset=0x%03"
                  HWADDR_PRIx "\n", bus->index, offset);
    return 0;
}

static void s5_rtos_i2c_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    S5I2C *bus = opaque;
    if ((offset & 3) || offset > 0x10) {
        bus->unknown_writes++;
        qemu_log_mask(LOG_UNIMP, "S5_I2C_UNSUPPORTED bus=%u write offset=0x%03"
                      HWADDR_PRIx " value=0x%08x\n", bus->index, offset,
                      (unsigned)value);
        return;
    }
    if (offset == 0) {
        /* Hardware owns pending; firmware clears it by writing bit 4 low.
         * Keep the full 32-bit control value, including the observed bit 8.
         */
        uint32_t pending = bus->regs[0] & value & 0x10;
        bus->regs[0] = (value & ~0x10U) | pending;
    } else {
        bus->regs[offset / 4] = value;
    }
    if (offset == 4 && ((value & 0xf0) == 0xf0 ||
                        (value & 0xf0) == 0xb0)) {
        bus->starts++;
        if (bus->machine->i2c_generic_error) {
            /* All four original ISR variants test status bit 3, then signal
             * their error event. Physical cause/category is not identified.
             * This is not an ACK, a fabricated peer reply, or a NACK claim.
             */
            bus->regs[1] |= 8;
            bus->regs[0] |= 0x10;
            bus->errors++;
        }
        if (bus->starts <= 64) {
            qemu_log_mask(LOG_UNIMP,
                "S5_I2C_START bus=%u data=0x%08x status=0x%08x control=0x%08x"
                " profile=%s\n", bus->index, bus->regs[3], bus->regs[1],
                bus->regs[0], bus->machine->i2c_generic_error ?
                "generic-controller-error" : "unmodeled-no-completion");
        }
    }
    s5_rtos_i2c_update(bus);
}

static const MemoryRegionOps s5_rtos_i2c_ops = {
    .read = s5_rtos_i2c_read,
    .write = s5_rtos_i2c_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static bool s5_rtos_get_i2c_error(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->i2c_generic_error;
}

static void s5_rtos_set_i2c_error(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->i2c_generic_error = value;
}

static char *s5_rtos_i2c_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new(NULL);
    unsigned i;
    g_string_append_printf(json, "{\"profile\":\"%s\","
        "\"peer_modeled\":false,\"physical_error_cause_verified\":false,"
        "\"successful_peer_replies\":0,\"buses\":[", s->i2c_generic_error ?
        "generic-controller-error" : "unmodeled-no-completion");
    for (i = 0; i < ARRAY_SIZE(s->i2c); i++) {
        S5I2C *bus = &s->i2c[i];
        g_string_append_printf(json,
            "%s{\"base\":\"0x%08" HWADDR_PRIx "\",\"gic_id\":%u,"
            "\"control\":%u,\"status\":%u,\"address\":%u,\"data\":%u,"
            "\"line_control\":%u,\"irq_level\":%s,\"starts\":%" PRIu64
            ",\"errors\":%" PRIu64 ",\"irq_assertions\":%" PRIu64
            ",\"irq_clears\":%" PRIu64 ",\"unknown_reads\":%" PRIu64
            ",\"unknown_writes\":%" PRIu64 "}", i ? "," : "", bus->base,
            bus->gic_id, bus->regs[0], bus->regs[1], bus->regs[2], bus->regs[3],
            bus->regs[4], bus->irq_level ? "true" : "false", bus->starts,
            bus->errors, bus->irq_assertions, bus->irq_clears,
            bus->unknown_reads, bus->unknown_writes);
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

static uint64_t s5_rtos_adc_read(void *opaque, hwaddr offset, unsigned size)
{
    S5ADC *adc = opaque;
    switch (offset) {
    case 0x00: return adc->control;
    case 0x04: return adc->scan;
    case 0x08: return adc->count;
    case 0x0c: return adc->mask;
    case 0x58: return adc->status;
    default:
        if (offset >= 0x14 && offset < 0x14 + adc->channels * 4 && !(offset & 3)) {
            return adc->results[(offset - 0x14) / 4];
        }
        adc->unknown_reads++;
        qemu_log_mask(LOG_UNIMP, "S5_ADC_UNSUPPORTED bank=%u read offset=0x%03"
                      HWADDR_PRIx "\n", adc->index, offset);
        return 0;
    }
}

static void s5_rtos_adc_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    S5ADC *adc = opaque;
    unsigned channel;
    switch (offset) {
    case 0x00:
        adc->control = value;
        if (value & 1) {
            adc->conversions++;
            adc->status |= 1;
            for (channel = 0; channel < adc->channels; channel++) {
                if (adc->mask & (1U << channel)) {
                    adc->results[channel] = adc->index == 0 && channel == 11 ?
                        adc->machine->adc11_sample : 0;
                }
            }
            /* Synchronous research conversion: only documented busy bits
             * clear. Physical timing, voltage scaling and IRQ mode unknown.
             */
            adc->control &= ~1U;
            adc->status &= ~1U;
            if (adc->conversions <= 64) {
                qemu_log_mask(LOG_UNIMP,
                    "S5_ADC_CONVERSION bank=%u mask=0x%08x count=%u raw11=%u"
                    " immediate-completion\n", adc->index, adc->mask,
                    adc->count, adc->machine->adc11_sample);
            }
        }
        break;
    case 0x04: adc->scan = value; break;
    case 0x08: adc->count = value; break;
    case 0x0c: adc->mask = value; break;
    default:
        adc->unknown_writes++;
        qemu_log_mask(LOG_UNIMP, "S5_ADC_UNSUPPORTED bank=%u write offset=0x%03"
                      HWADDR_PRIx " value=0x%08x ignored\n", adc->index,
                      offset, (unsigned)value);
    }
}

static const MemoryRegionOps s5_rtos_adc_ops = {
    .read = s5_rtos_adc_read,
    .write = s5_rtos_adc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void s5_rtos_get_adc11_sample(Object *obj, Visitor *v, const char *name,
                                   void *opaque, Error **errp)
{
    uint32_t value = S5_RTOS_MACHINE(obj)->adc11_sample;
    visit_type_uint32(v, name, &value, errp);
}

static void s5_rtos_set_adc11_sample(Object *obj, Visitor *v, const char *name,
                                   void *opaque, Error **errp)
{
    uint32_t value;
    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    if (value > 0xffff) {
        error_setg(errp, "adc11-sample must fit the original 16-bit raw result");
        return;
    }
    S5_RTOS_MACHINE(obj)->adc11_sample = value;
}

static char *s5_rtos_adc_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new(NULL);
    unsigned i, channel;
    g_string_append_printf(json, "{\"profile\":\"synchronous-raw-input\","
        "\"voltage_scale_known\":false,\"conversion_latency_verified\":false,"
        "\"adc11_sample\":%u,\"banks\":[", s->adc11_sample);
    for (i = 0; i < ARRAY_SIZE(s->adc); i++) {
        S5ADC *adc = &s->adc[i];
        g_string_append_printf(json, "%s{\"base\":\"0x%08" HWADDR_PRIx
            "\",\"control\":%u,\"mask\":%u,\"count\":%u,\"status\":%u,"
            "\"conversions\":%" PRIu64 ",\"unknown_reads\":%" PRIu64
            ",\"unknown_writes\":%" PRIu64 ",\"results\":[", i ? "," : "",
            adc->base, adc->control, adc->mask, adc->count, adc->status,
            adc->conversions, adc->unknown_reads, adc->unknown_writes);
        for (channel = 0; channel < adc->channels; channel++) {
            g_string_append_printf(json, "%s%u", channel ? "," : "",
                                   adc->results[channel]);
        }
        g_string_append(json, "]}");
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

static void s5_rtos_spi_data_access(S5SPIProbe *s, uint64_t value,
                                    unsigned size, bool write)
{
    s->data_accesses++;
    s->last_data = value;
    s->last_size = size;
    s->last_write = write;
    s->last_cpu = current_cpu ? current_cpu->cpu_index : -1;
    s->last_pc = current_cpu ? CPU_GET_CLASS(current_cpu)->get_pc(current_cpu) : 0;
    if (s->data_accesses <= 128) {
        qemu_log_mask(LOG_UNIMP,
                  "S5_SPI%u_UNSUPPORTED %s cpu=%d pc=0x%08x size=%u value=0x%08x"
                  " cs=0x%08x mode=%s\n",
                  s->index, write ? "TX" : "RX-read", s->last_cpu,
                  s->last_pc, size, (unsigned)value, s->cs_value,
                  s->open_bus ? "absent-peer-assume-MISO-low" :
                                    "debug-stop-no-RX-response");
    }
    if (!s->open_bus && current_cpu) {
        s->strict_stops++;
        cpu_interrupt(current_cpu, CPU_INTERRUPT_DEBUG);
    }
}

static uint64_t s5_rtos_spi_data_read(void *opaque, hwaddr offset, unsigned size)
{
    S5SPIProbe *s = opaque;
    uint64_t value = 0;
    if (s->open_bus) {
        memory_region_dispatch_read(s->controller_region, offset + 8,
                                    &value, size_memop(size) | MO_LE,
                                    MEMTXATTRS_UNSPECIFIED);
        s->open_bus_reads++;
    }
    s5_rtos_spi_data_access(s, value, size, false);
    return value;
}

static void s5_rtos_spi_data_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size)
{
    S5SPIProbe *s = opaque;
    s5_rtos_spi_data_access(s, value, size, true);
    if (s->open_bus) {
        memory_region_dispatch_write(s->controller_region, offset + 8,
                                     value, size_memop(size) | MO_LE,
                                     MEMTXATTRS_UNSPECIFIED);
    }
}

static const MemoryRegionOps s5_rtos_spi_data_ops = {
    .read = s5_rtos_spi_data_read,
    .write = s5_rtos_spi_data_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static uint64_t s5_rtos_spi0_cs_read(void *opaque, hwaddr offset, unsigned size)
{
    S5SPIProbe *s = opaque;
    return s->cs_value >> (offset * 8);
}

static void s5_rtos_spi0_cs_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    S5SPIProbe *s = opaque;
    s->cs_writes++;
    s->cs_value = value << (offset * 8);
    if (s->cs_writes <= 64) {
        qemu_log_mask(LOG_UNIMP, "S5_SPI0_CS encoded-write=0x%08x size=%u\n",
                      s->cs_value, size);
    }
}

static const MemoryRegionOps s5_rtos_spi0_cs_ops = {
    .read = s5_rtos_spi0_cs_read,
    .write = s5_rtos_spi0_cs_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static char *s5_rtos_spi_state_for(S5SPIProbe *s)
{
    return g_strdup_printf(
        "{\"channel\":%u,\"profile\":\"%s\",\"peer_modeled\":false,"
        "\"strict_stops\":%" PRIu64 ",\"open_bus_rx_reads\":%" PRIu64
        ",\"cs_writes\":%" PRIu64
        ",\"cs_encoded_value\":%u,\"data_accesses\":%" PRIu64
        ",\"last_access\":{\"operation\":\"%s\",\"cpu\":%d,"
        "\"pc\":%u,\"size\":%u,\"value\":%u}}",
        s->index, s->open_bus ? "absent-peer-assume-MISO-low" :
                          "strict-unimplemented-data-endpoint",
        s->strict_stops, s->open_bus_reads,
        s->cs_writes, s->cs_value, s->data_accesses,
        s->last_write ? "write" : "read", s->last_cpu,
        s->last_pc, s->last_size, s->last_data);
}

static char *s5_rtos_spi0_state(Object *obj, Error **errp)
{
    return s5_rtos_spi_state_for(&S5_RTOS_MACHINE(obj)->spi_probes[0]);
}

static char *s5_rtos_spi6_state(Object *obj, Error **errp)
{
    return s5_rtos_spi_state_for(&S5_RTOS_MACHINE(obj)->spi_probes[6]);
}

static bool s5_rtos_get_spi0_open_bus(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->spi_probes[0].open_bus;
}

static void s5_rtos_set_spi0_open_bus(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->spi_probes[0].open_bus = value;
}

static bool s5_rtos_get_spi6_open_bus(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->spi_probes[6].open_bus;
}

static void s5_rtos_set_spi6_open_bus(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->spi_probes[6].open_bus = value;
}

static void s5_rtos_get_spi_mask(Object *obj, Visitor *v, const char *name,
                               void *opaque, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    uint32_t value = 0;
    unsigned i;
    for (i = 0; i < 7; i++) {
        if (i != 3 && s->spi_probes[i].open_bus) {
            value |= 1U << i;
        }
    }
    visit_type_uint32(v, name, &value, errp);
}

static void s5_rtos_set_spi_mask(Object *obj, Visitor *v, const char *name,
                               void *opaque, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    uint32_t value;
    unsigned i;
    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    if (value & ~0x77U) {
        error_setg(errp, "spi-open-bus-mask allows channels 0,1,2,4,5,6 only; RTC3 retains its peer");
        return;
    }
    for (i = 0; i < 7; i++) {
        if (i != 3) {
            s->spi_probes[i].open_bus = (value & (1U << i)) != 0;
        }
    }
}

static char *s5_rtos_spi_states(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new("{\"channels\":[");
    unsigned i;
    for (i = 0; i < 7; i++) {
        if (i) {
            g_string_append_c(json, ',');
        }
        if (i == 3) {
            g_string_append(json, "{\"channel\":3,\"profile\":\"rtc-register-peer\"}");
        } else {
            char *state = s5_rtos_spi_state_for(&s->spi_probes[i]);
            g_string_append(json, state);
            g_free(state);
        }
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

static uint64_t s5_rtos_input_read(void *opaque, hwaddr offset, unsigned size)
{
    S5GPIOInput *input = opaque;
    return (input->machine->inactive_inputs ? 0x10 : 0) >> (offset * 8);
}

static void s5_rtos_input_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    S5GPIOInput *input = opaque;
    qemu_log_mask(LOG_UNIMP,
                  "S5_GPIO_INPUT write addr=0x%08" HWADDR_PRIx
                  " size=%u value=0x%" PRIx64 " ignored\n",
                  input->base + offset, size, value);
}

static const MemoryRegionOps s5_rtos_input_ops = {
    .read = s5_rtos_input_read,
    .write = s5_rtos_input_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static bool s5_rtos_get_inactive_inputs(Object *obj, Error **errp)
{
    return S5_RTOS_MACHINE(obj)->inactive_inputs;
}

static void s5_rtos_set_inactive_inputs(Object *obj, bool value, Error **errp)
{
    S5_RTOS_MACHINE(obj)->inactive_inputs = value;
}

static char *s5_rtos_gic_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GICState *gic;
    GString *json;
    unsigned i;
    bool comma = false;
    if (!s->gic) {
        return g_strdup("{\"initialized\":false}");
    }
    gic = ARM_GIC_COMMON(s->gic);
    json = g_string_new(NULL);
    g_string_append_printf(json, "{\"distributor_control\":%u,"
                            "\"security_extensions\":%s,\"cpus\":[",
                            gic->ctlr, gic->security_extn ? "true" : "false");
    for (i = 0; i < S5_RTOS_CPUS; i++) {
        CPUState *cpu = qemu_get_cpu(i);
        g_string_append_printf(json,
            "%s{\"index\":%u,\"control\":%u,\"priority_mask\":%u,"
            "\"running_priority\":%u,\"current_pending\":%u,"
            "\"halted\":%s,\"interrupt_request\":%u}", i ? "," : "", i,
            gic->cpu_ctlr[i], gic->priority_mask[i], gic->running_priority[i],
            gic->current_pending[i], cpu->halted ? "true" : "false",
            cpu->interrupt_request);
    }
    g_string_append(json, "],\"enabled_or_asserted_irqs\":[");
    for (i = 0; i < S5_RTOS_IRQS; i++) {
        gic_irq_state *irq = &gic->irq_state[i];
        if (irq->enabled || irq->pending || irq->active || irq->level) {
            g_string_append_printf(json,
                "%s{\"id\":%u,\"enabled\":%u,\"pending\":%u,\"active\":%u,"
                "\"level\":%u,\"group\":%u,\"targets\":%u,\"priority_cpu0\":%u}",
                comma ? "," : "", i, irq->enabled, irq->pending, irq->active,
                irq->level, irq->group, gic->irq_target[i],
                i < GIC_INTERNAL ? gic->priority1[i][0] :
                                   gic->priority2[i - GIC_INTERNAL]);
            comma = true;
        }
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

/* Original table at 0x06fefaa0: menu-smp/module-handshake-evidence.json.
 * Completion latency and error states are not known.  This model reflects a
 * request immediately in only the corresponding status bits used by firmware.
 */
static bool s5_rtos_module_control(hwaddr offset)
{
    return offset == 0 ||
        (offset >= 0x14 && offset <= 0x5c && !(offset & 3)) ||
        (offset >= 0x6c && offset <= 0x98 && !(offset & 3));
}

static bool s5_rtos_module_reset(hwaddr offset)
{
    return offset >= 0xc0 && offset <= 0x124 && !(offset & 3);
}

static uint64_t s5_rtos_module_read(void *opaque, hwaddr offset, unsigned size)
{
    S5RTOSMachineState *s = opaque;
    if (!s5_rtos_module_control(offset) && !s5_rtos_module_reset(offset)) {
        if (s->module_unknown_reads++ < 256) {
            qemu_log_mask(LOG_UNIMP,
                          "S5_MODULE_UNSUPPORTED read offset=0x%03" HWADDR_PRIx
                          " value=0x%08x\n", offset, s->module_regs[offset / 4]);
        }
    }
    return s->module_regs[offset / 4];
}

static void s5_rtos_module_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    S5RTOSMachineState *s = opaque;
    uint32_t result = value;
    if (s5_rtos_module_control(offset)) {
        result = (value & ~0xf) | ((value >> 4) & 0xf);
        s->module_requests++;
    } else if (s5_rtos_module_reset(offset)) {
        result = (value & ~0x110) | (value & 1 ? 0x110 : 0);
        s->module_requests++;
    } else {
        s->module_unknown_writes++;
        qemu_log_mask(LOG_UNIMP,
                      "S5_MODULE_UNSUPPORTED write offset=0x%03" HWADDR_PRIx
                      " value=0x%08x\n", offset, (unsigned)value);
    }
    s->module_regs[offset / 4] = result;
    qemu_log_mask(LOG_UNIMP,
                  "S5_MODULE_REQUEST offset=0x%03" HWADDR_PRIx
                  " requested=0x%08x immediate-result=0x%08x\n",
                  offset, (unsigned)value, result);
}

static const MemoryRegionOps s5_rtos_module_ops = {
    .read = s5_rtos_module_read,
    .write = s5_rtos_module_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static char *s5_rtos_module_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new(NULL);
    unsigned i;
    bool comma = false;
    g_string_append_printf(json,
        "{\"profile\":\"inferred-immediate-request-ack\","
        "\"physical_latency_verified\":false,\"requests\":%" PRIu64
        ",\"unknown_reads\":%" PRIu64 ",\"unknown_writes\":%" PRIu64
        ",\"nonzero_registers\":{", s->module_requests,
        s->module_unknown_reads, s->module_unknown_writes);
    for (i = 0; i < ARRAY_SIZE(s->module_regs); i++) {
        if (s->module_regs[i]) {
            g_string_append_printf(json, "%s\"0x%03x\":%u", comma ? "," : "",
                                   i * 4, s->module_regs[i]);
            comma = true;
        }
    }
    g_string_append(json, "}}");
    return g_string_free(json, false);
}

static uint64_t s5_rtos_spi_cs_read(void *opaque, hwaddr offset, unsigned size)
{
    S5RTOSMachineState *s = opaque;
    /* Readback is an experiment convenience; the observed driver only writes. */
    return s->spi_cs_value;
}

static void s5_rtos_spi_cs_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    S5RTOSMachineState *s = opaque;
    if (value == 0x10000001 || value == 0x10000000) {
        s->spi_cs_value = value;
        qemu_set_irq(qdev_get_gpio_in_named(s->rtc_peer, SSI_GPIO_CS, 0),
                     value & 1);
        qemu_log_mask(LOG_UNIMP, "S5_RTC_CS selected=%u encoded=0x%08x\n",
                      (unsigned)(value & 1), (unsigned)value);
    } else {
        qemu_log_mask(LOG_UNIMP,
                      "S5_RTC_UNSUPPORTED cs-write=0x%08" PRIx64 "\n", value);
    }
}

static const MemoryRegionOps s5_rtos_spi_cs_ops = {
    .read = s5_rtos_spi_cs_read,
    .write = s5_rtos_spi_cs_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

static void s5_rtos_record_unknown(S5RTOSMachineState *s, hwaddr offset,
                                  unsigned size, bool write, uint64_t value)
{
    int cpu = current_cpu ? current_cpu->cpu_index : -1;
    gpointer key = GSIZE_TO_POINTER((offset << 8) | ((cpu + 1) << 5) |
                                    (size << 1) | write);
    S5UnknownAccess *access = g_hash_table_lookup(s->unknown_index, key);
    s->unknown_total++;
    if (!access) {
        if (s->unknown_unique == ARRAY_SIZE(s->unknown_accesses)) {
            s->unknown_dropped++;
            return;
        }
        access = &s->unknown_accesses[s->unknown_unique++];
        access->address = offset + 0x70000000;
        access->cpu = cpu;
        access->size = size;
        access->write = write;
        g_hash_table_insert(s->unknown_index, key, access);
    }
    access->count++;
    access->last_value = value;
    if (access->count <= 4) {
        qemu_log_mask(LOG_UNIMP, "S5_RTOS_UNMODELED cpu=%d %s addr=0x%08"
                      HWADDR_PRIx " size=%u value=0x%" PRIx64
                      " occurrence=%" PRIu64 " %s\n", cpu,
                      write ? "write" : "read", access->address, size,
                      value, access->count, write ? "ignored" : "returned");
    }
}

static uint64_t s5_rtos_unknown_read(void *opaque, hwaddr offset, unsigned size)
{
    S5RTOSMachineState *s = opaque;
    static const hwaddr test_offsets[] = { 0x0c00011c, 0x0c050ef0 };
    static const uint32_t test_bits[] = { 1, 1U << 28 };
    uint64_t value = 0;
    unsigned i;

    if (s->serial_tx.enabled && offset >= 0x0b193400 &&
        offset < 0x0b19344c) {
        return s5_serial_tx_read(&s->serial_tx, offset - 0x0b193400, size);
    }

    for (i = 0; i < ARRAY_SIZE(test_offsets); i++) {
        if (offset >= test_offsets[i] &&
            offset + size <= test_offsets[i] + 4) {
            s->display_test_reads[i]++;
            if (s->display_test_mode &&
                (s->display_test_status_mask & (1U << i))) {
                value = (test_bits[i] >> ((offset - test_offsets[i]) * 8)) &
                        ((UINT64_C(1) << (size * 8)) - 1);
            }
            break;
        }
    }
    s5_rtos_record_unknown(s, offset, size, false, value);
    return value;
}

static void s5_rtos_unknown_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size)
{
    S5RTOSMachineState *s = opaque;
    if (s->serial_tx.enabled && offset >= 0x0b193400 &&
        offset < 0x0b19344c) {
        s5_serial_tx_write(&s->serial_tx, offset - 0x0b193400, value, size);
        return;
    }
    s5_rtos_record_unknown(opaque, offset, size, true, value);
}

static char *s5_rtos_unknown_state(Object *obj, Error **errp)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(obj);
    GString *json = g_string_new(NULL);
    unsigned i;
    g_string_append_printf(json, "{\"total\":%" PRIu64
                            ",\"unique\":%u,\"dropped\":%" PRIu64
                            ",\"accesses\":[", s->unknown_total,
                            s->unknown_unique, s->unknown_dropped);
    for (i = 0; i < s->unknown_unique; i++) {
        S5UnknownAccess *a = &s->unknown_accesses[i];
        g_string_append_printf(json, "%s{\"cpu\":%d,\"address\":\"0x%08"
            HWADDR_PRIx "\",\"size\":%u,\"operation\":\"%s\",\"count\":%"
            PRIu64 ",\"last_value\":%" PRIu64 "}", i ? "," : "", a->cpu,
            a->address, a->size, a->write ? "write" : "read", a->count,
            a->last_value);
    }
    g_string_append(json, "]}");
    return g_string_free(json, false);
}

static const MemoryRegionOps s5_rtos_unknown_ops = {
    .read = s5_rtos_unknown_read,
    .write = s5_rtos_unknown_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

static void s5_rtos_init(MachineState *machine)
{
    S5RTOSMachineState *s = S5_RTOS_MACHINE(machine);
    MemoryRegion *sysmem = get_system_memory();
    DeviceState *cpus[S5_RTOS_CPUS];
    DeviceState *gic, *timer, *spi;
    SysBusDevice *gicbus, *timerbus, *spibus;
    unsigned i;

    if (machine->ram_size > 2 * GiB) {
        error_report("s5-rtos RAM must be at most 2 GiB");
        exit(EXIT_FAILURE);
    }
    /* The original conversion table 0x06ff11b0 exposes CPU high aliases for
     * two 1-GiB banks, but a low/cached alias only for the first bank. Keep
     * 0x40000000..0x7fffffff free of DRAM (including SoC MMIO). Opting into
     * -m 2G supplies actual distinct backing for the original software arena;
     * it does not establish the camera's physical memory capacity. Never
     * fold high pointers into the smaller default 512-MiB experiment RAM.
     */
    memory_region_init_alias(&s->low_alias, OBJECT(machine),
                             "s5-rtos.ram-low-alias", machine->ram, 0,
                             MIN(machine->ram_size, 1 * GiB));
    memory_region_add_subregion(sysmem, 0, &s->low_alias);
    memory_region_init_alias(&s->high_alias, OBJECT(machine),
                             "s5-rtos.ram-high-alias", machine->ram, 0,
                             machine->ram_size);
    memory_region_add_subregion(sysmem, 0x80000000, &s->high_alias);

    /* Unknown SoC registers remain observable zero/discard placeholders. */
    memory_region_init_io(&s->unknown_mmio, OBJECT(machine),
                          &s5_rtos_unknown_ops, s, "s5-rtos.unmodeled-mmio",
                          0x10000000);
    memory_region_add_subregion_overlap(sysmem, 0x70000000,
                                        &s->unknown_mmio, -100);
    memory_region_init_io(&s->software_status, OBJECT(machine),
                          &s5_rtos_software_status_ops, s,
                          "s5-rtos.software-status", 16);
    memory_region_add_subregion(sysmem, 0x7c0151f4, &s->software_status);
    memory_region_init_ram_nomigrate(&s->pool_sram, OBJECT(machine),
                                    "s5-rtos.pool-sram", 0x4000, &error_fatal);
    memory_region_add_subregion(sysmem, 0x70028000, &s->pool_sram);
    /* Original RTOS uploads 0x20000 data bytes to 0x7b000000 and 0x10000
     * code bytes to 0x7b020000. This stores those exact bytes only: there is
     * no guessed coprocessor CPU, ready flag, magic value or completion IRQ.
     */
    memory_region_init_ram_nomigrate(&s->coprocessor_sram, OBJECT(machine),
        "s5-rtos.coprocessor-upload-sram", 0x30000, &error_fatal);
    memory_region_add_subregion(sysmem, 0x7b000000, &s->coprocessor_sram);
    memory_region_init_io(&s->module_controls, OBJECT(machine),
                          &s5_rtos_module_ops, s, "s5-rtos.module-controls",
                          0x1000);
    memory_region_add_subregion(sysmem, 0x7c160000, &s->module_controls);
    for (i = 0; i < ARRAY_SIZE(s->adc); i++) {
        const unsigned channels[] = { 12, 7, 4 };
        S5ADC *adc = &s->adc[i];
        adc->machine = s;
        adc->base = 0x7f030000 + i * 0x1000;
        adc->index = i;
        adc->channels = channels[i];
        memory_region_init_io(&adc->iomem, OBJECT(machine), &s5_rtos_adc_ops,
                              adc, "s5-rtos.adc", 0x1000);
        memory_region_add_subregion(sysmem, adc->base, &adc->iomem);
    }
    /* The original startup task debounces bit 4 at both addresses.  Raising
     * these inputs is an explicit no-special-startup-input profile; physical
     * button names and actual reset voltage levels have not been established.
     */
    for (i = 0; i < ARRAY_SIZE(s->startup_inputs); i++) {
        S5GPIOInput *input = &s->startup_inputs[i];
        input->machine = s;
        input->base = i ? 0x7f080410 : 0x7f080094;
        memory_region_init_io(&input->iomem, OBJECT(machine), &s5_rtos_input_ops,
                              input, "s5-rtos.startup-gpio-input", 4);
        memory_region_add_subregion(sysmem, input->base, &input->iomem);
    }

    for (i = 0; i < S5_RTOS_CPUS; i++) {
        Object *cpuobj = object_new(machine->cpu_type);
        object_property_set_bool(cpuobj, "has_el3", false, &error_fatal);
        object_property_set_bool(cpuobj, "has_el2", false, &error_fatal);
        object_property_set_int(cpuobj, "reset-cbar", 0x70000000, &error_fatal);
        object_property_set_int(cpuobj, "mp-affinity", i, &error_fatal);
        cpus[i] = DEVICE(cpuobj);
        qdev_realize(cpus[i], NULL, &error_fatal);
    }

    gic = qdev_new(TYPE_ARM_GIC);
    s->gic = gic;
    qdev_prop_set_uint32(gic, "revision", 2);
    qdev_prop_set_uint32(gic, "num-cpu", S5_RTOS_CPUS);
    qdev_prop_set_uint32(gic, "num-irq", S5_RTOS_IRQS);
    qdev_prop_set_bit(gic, "has-security-extensions", false);
    qdev_prop_set_bit(gic, "has-virtualization-extensions", false);
    gicbus = SYS_BUS_DEVICE(gic);
    sysbus_realize_and_unref(gicbus, &error_fatal);
    sysbus_mmio_map(gicbus, 0, 0x70001000);
    /* Loader and original RTOS IAR/EOIR routines address each CPU interface
     * at base + MPIDR[1:0] * 0x8000, rather than using one banked address.
     * With virtualization disabled, MMIO slots 2..5 are QEMU's fixed CPU
     * views. The distributor stays shared, with matching address aliases.
     */
    for (i = 0; i < S5_RTOS_CPUS; i++) {
        sysbus_mmio_map(gicbus, 2 + i, 0x70002000 + i * 0x8000);
        if (i) {
            memory_region_init_alias(&s->gic_dist_alias[i - 1], OBJECT(machine),
                "s5-rtos.gic-distributor-alias", sysbus_mmio_get_region(gicbus, 0),
                0, 0x1000);
            memory_region_add_subregion(sysmem, 0x70001000 + i * 0x8000,
                                        &s->gic_dist_alias[i - 1]);
        }
    }

    for (i = 0; i < S5_RTOS_CPUS; i++) {
        const unsigned timer_irqs[] = {
            [GTIMER_PHYS] = 30, [GTIMER_VIRT] = 27,
            [GTIMER_HYP] = 26, [GTIMER_SEC] = 29,
        };
        unsigned irq, ppi_base = S5_RTOS_IRQS - GIC_INTERNAL + i * GIC_INTERNAL;

        for (irq = 0; irq < ARRAY_SIZE(timer_irqs); irq++) {
            qdev_connect_gpio_out(cpus[i], irq,
                                 qdev_get_gpio_in(gic, ppi_base + timer_irqs[irq]));
        }
        sysbus_connect_irq(gicbus, i, qdev_get_gpio_in(cpus[i], ARM_CPU_IRQ));
        sysbus_connect_irq(gicbus, i + S5_RTOS_CPUS,
                           qdev_get_gpio_in(cpus[i], ARM_CPU_FIQ));
        sysbus_connect_irq(gicbus, i + 2 * S5_RTOS_CPUS,
                           qdev_get_gpio_in(cpus[i], ARM_CPU_VIRQ));
        sysbus_connect_irq(gicbus, i + 3 * S5_RTOS_CPUS,
                           qdev_get_gpio_in(cpus[i], ARM_CPU_VFIQ));
    }

    /* Original base table 0x06ff1188 and logical IDs 0x7c..0x80 through
     * 0x06ff1240 map these five controllers to GIC IDs 282..286.
     */
    for (i = 0; i < ARRAY_SIZE(s->i2c); i++) {
        S5I2C *bus = &s->i2c[i];
        bus->machine = s;
        bus->base = 0x7f058000 + i * 0x100;
        bus->index = i;
        bus->gic_id = 282 + i;
        bus->irq = qdev_get_gpio_in(gic, bus->gic_id - GIC_INTERNAL);
        memory_region_init_io(&bus->iomem, OBJECT(machine), &s5_rtos_i2c_ops,
                              bus, "s5-rtos.i2c-error-controller", 0x100);
        memory_region_add_subregion(sysmem, bus->base, &bus->iomem);
    }

    /* Original tables 0x08ab4778 / 0x08ab478c and logical IRQ map 0x06ff1240.
     * IRQs are deliberately not guessed as a contiguous sequence.
     */
    for (i = 0; i < 5; i++) {
        const unsigned uart_ids[] = { 85, 86, 294, 295, 296 };
        Chardev *chr = serial_hd(i);
        if (!chr) {
            ChardevRingbuf options = { .has_size = true, .size = 1 * MiB };
            ChardevBackend backend = { .type = CHARDEV_BACKEND_KIND_RINGBUF,
                                       .u.ringbuf.data = &options };
            char *id = g_strdup_printf("s5-uart%u", i);
            chr = qemu_chardev_new(id, "chardev-ringbuf", &backend, NULL,
                                   &error_fatal);
            g_free(id);
        }
        pl011_create(0x7f050000 + i * 0x1000,
                     qdev_get_gpio_in(gic, uart_ids[i] - GIC_INTERNAL), chr);
    }
    /* The original SSP table has seven PL022-compatible channels. Channel 3
     * has the separately reconstructed RTC peer; unidentified peers stop at
     * DR by default, before PL022 can enqueue an invented response.
     */
    for (i = 0; i < 7; i++) {
        S5SPIProbe *probe = &s->spi_probes[i];
        char *name;
        probe->index = i;
        if (i == 3) {
            continue;
        }
        name = g_strdup_printf("spi%u", i);
        spi = qdev_new(TYPE_PL022);
        object_property_add_child(OBJECT(machine), name, OBJECT(spi));
        spibus = SYS_BUS_DEVICE(spi);
        sysbus_realize_and_unref(spibus, &error_fatal);
        sysbus_mmio_map(spibus, 0, 0x7f040000 + i * 0x1000);
        probe->controller_region = sysbus_mmio_get_region(spibus, 0);
        memory_region_init_io(&probe->data_gate, OBJECT(machine),
                              &s5_rtos_spi_data_ops, probe, name, 4);
        memory_region_add_subregion_overlap(sysmem, 0x7f040008 + i * 0x1000,
                                            &probe->data_gate, 10);
        g_free(name);
    }
    memory_region_init_io(&s->spi0_cs_capture, OBJECT(machine),
                          &s5_rtos_spi0_cs_ops, &s->spi_probes[0],
                          "s5-rtos.spi0-cs-capture", 4);
    memory_region_add_subregion(sysmem, 0x7f081c00, &s->spi0_cs_capture);
    /* Original table 0x06ff2a50 contains 13 pairs at base and base + 0x20.
     * The driver at 0x06554d8c reads VALUE and interprets the SP804 prescaler;
     * its time conversion establishes a 27 MHz input.  The logical IRQ table
     * at 0x06ff2ab8 and GIC mapping at 0x06ff1240 give IDs 36 through 61.
     */
    for (i = 0; i < 13; i++) {
        timer = qdev_new("sp804");
        qdev_prop_set_uint32(timer, "freq0", 27000000);
        qdev_prop_set_uint32(timer, "freq1", 27000000);
        /* Original RTOS forces CTRL bit 4, configures edge-triggered GIC,
         * and its timer ISR never writes INTCLR. Preserve latched status,
         * but infer one output pulse per actual expiry in that mode.
         * Standard SP804 and s5-linux retain the property's false default.
         */
        qdev_prop_set_bit(timer, "s5-inferred-pulse-bit4", true);
        qdev_prop_set_bit(timer, "load-rearms-oneshot", true);
        timerbus = SYS_BUS_DEVICE(timer);
        sysbus_realize_and_unref(timerbus, &error_fatal);
        sysbus_mmio_map(timerbus, 0, 0x7f020000 + 0x1000 * i);
        /* Output 0 remains the stock combined line, for s5-linux and other
         * boards.  The compatible extension exposes timer 0/1 on lines 1/2.
         */
        sysbus_connect_irq(timerbus, 1, qdev_get_gpio_in(gic, 4 + 2 * i));
        sysbus_connect_irq(timerbus, 2, qdev_get_gpio_in(gic, 5 + 2 * i));
    }

    /* Register layout and polling bits match PL022.  The attached register
     * protocol is reconstructed from original firmware, with explicit assumed
     * cold-reset values rather than a claim to emulate an identified chip.
     * The SPI interrupt number has not yet been established, so is unwired.
     */
    spi = qdev_new(TYPE_PL022);
    object_property_add_child(OBJECT(machine), "spi3", OBJECT(spi));
    spibus = SYS_BUS_DEVICE(spi);
    sysbus_realize_and_unref(spibus, &error_fatal);
    sysbus_mmio_map(spibus, 0, 0x7f043000);
    s->rtc_peer = qdev_new("s5-rtc-register-peer");
    object_property_add_child(OBJECT(machine), "rtc-peer", OBJECT(s->rtc_peer));
    ssi_realize_and_unref(s->rtc_peer, PL022(spi)->ssi, &error_fatal);
    memory_region_init_io(&s->spi_cs, OBJECT(machine), &s5_rtos_spi_cs_ops,
                          s, "s5-rtos.spi3-cs-bridge", 4);
    memory_region_add_subregion(sysmem, 0x7f081c0c, &s->spi_cs);
    qemu_log_mask(LOG_UNIMP, "S5_RTOS SPI3 peer uses assumed cold RTC state\n");
}

static void s5_rtos_machine_class_init(ObjectClass *oc, void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);
    mc->desc = "Panasonic S5 RTOS SMP research machine (partial, V2.9)";
    mc->init = s5_rtos_init;
    mc->min_cpus = mc->max_cpus = mc->default_cpus = S5_RTOS_CPUS;
    mc->default_cpu_type = ARM_CPU_TYPE_NAME("cortex-a7");
    /* Experiment capacity, not a verified statement of physical camera RAM. */
    mc->default_ram_size = 512 * MiB;
    mc->default_ram_id = "s5-rtos.ram";
    mc->ignore_memory_transaction_failures = false;
    object_class_property_add_str(oc, "module-state-json",
                                  s5_rtos_module_state, NULL);
    object_class_property_add_str(oc, "software-status-state-json",
                                  s5_rtos_software_status_state, NULL);
    object_class_property_add_str(oc, "gic-state-json", s5_rtos_gic_state, NULL);
    object_class_property_add_str(oc, "ram-layout-json", s5_rtos_ram_layout,
                                  NULL);
    object_class_property_add_str(oc, "unmodeled-mmio-json",
                                  s5_rtos_unknown_state, NULL);
    object_class_property_add_str(oc, "spi0-state-json", s5_rtos_spi0_state, NULL);
    object_class_property_add_bool(oc, "spi0-open-bus", s5_rtos_get_spi0_open_bus,
                                   s5_rtos_set_spi0_open_bus);
    object_class_property_add_str(oc, "spi6-state-json", s5_rtos_spi6_state, NULL);
    object_class_property_add_bool(oc, "spi6-open-bus", s5_rtos_get_spi6_open_bus,
                                   s5_rtos_set_spi6_open_bus);
    object_class_property_add(oc, "spi-open-bus-mask", "uint32",
                              s5_rtos_get_spi_mask, s5_rtos_set_spi_mask,
                              NULL, NULL);
    object_class_property_add_str(oc, "spi-state-json", s5_rtos_spi_states, NULL);
    object_class_property_add(oc, "adc11-sample", "uint32",
                              s5_rtos_get_adc11_sample, s5_rtos_set_adc11_sample,
                              NULL, NULL);
    object_class_property_add_str(oc, "adc-state-json", s5_rtos_adc_state, NULL);
    object_class_property_add_bool(oc, "i2c-generic-error", s5_rtos_get_i2c_error,
                                   s5_rtos_set_i2c_error);
    object_class_property_add_str(oc, "i2c-state-json", s5_rtos_i2c_state, NULL);
    object_class_property_add_bool(oc, "display-test-mode",
                                   s5_rtos_get_display_test_mode,
                                   s5_rtos_set_display_test_mode);
    object_class_property_add(oc, "display-test-status-mask", "uint32",
                              s5_rtos_get_display_test_mask,
                              s5_rtos_set_display_test_mask, NULL, NULL);
    object_class_property_add(oc, "display-test-pulse", "uint32", NULL,
                              s5_rtos_set_display_test_pulse, NULL, NULL);
    object_class_property_add_str(oc, "display-test-state-json",
                                  s5_rtos_display_test_state, NULL);
    object_class_property_add_bool(oc, "frame-test-mode",
                                   s5_rtos_get_frame_test_mode,
                                   s5_rtos_set_frame_test_mode);
    object_class_property_add(oc, "frame-test-pulse", "uint32", NULL,
                              s5_rtos_set_frame_test_pulse, NULL, NULL);
    object_class_property_add_str(oc, "frame-test-state-json",
                                  s5_rtos_frame_test_state, NULL);
    object_class_property_add_bool(oc, "tx-serial-hle",
                                   s5_rtos_get_serial_tx_hle,
                                   s5_rtos_set_serial_tx_hle);
    object_class_property_add_str(oc, "tx-serial-state-json",
                                  s5_rtos_serial_tx_state, NULL);
    object_class_property_add_bool(oc, "inactive-inputs",
                                   s5_rtos_get_inactive_inputs,
                                   s5_rtos_set_inactive_inputs);
}

static void s5_rtos_machine_instance_init(Object *obj)
{
    S5_RTOS_MACHINE(obj)->inactive_inputs = true;
    S5_RTOS_MACHINE(obj)->i2c_generic_error = true;
    S5_RTOS_MACHINE(obj)->unknown_index = g_hash_table_new(g_direct_hash,
                                                         g_direct_equal);
}

static void s5_rtos_machine_finalize(Object *obj)
{
    g_hash_table_destroy(S5_RTOS_MACHINE(obj)->unknown_index);
}

static const TypeInfo s5_rtos_machine_type = {
    .name = TYPE_S5_RTOS_MACHINE,
    .parent = TYPE_MACHINE,
    .instance_size = sizeof(S5RTOSMachineState),
    .instance_init = s5_rtos_machine_instance_init,
    .instance_finalize = s5_rtos_machine_finalize,
    .class_init = s5_rtos_machine_class_init,
};

static void s5_rtos_machine_register_types(void)
{
    type_register_static(&s5_rtos_machine_type);
}

type_init(s5_rtos_machine_register_types)
