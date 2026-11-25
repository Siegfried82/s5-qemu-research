/*
 * S5 SPI3 RTC/backup register peer reconstructed from V2.9 firmware calls.
 * SPDX-License-Identifier: GPL-2.0-or-later
 *
 * This is a protocol research model, not an identified physical chip.  Its
 * cold-reset values are explicit experimental assumptions.  Unknown reads
 * are reported rather than silently treated as verified device behavior.
 */
#include "qemu/osdep.h"
#include "qapi/error.h"
#include "qemu/log.h"
#include "hw/ssi/ssi.h"

#define TYPE_S5_RTC_PEER "s5-rtc-register-peer"
OBJECT_DECLARE_SIMPLE_TYPE(S5RTCPeer, S5_RTC_PEER)

struct S5RTCPeer {
    SSIPeripheral parent_obj;
    uint8_t registers[0x8000];
    bool defined[0x8000];
    bool unsupported[0x8000];
    uint16_t command;
    unsigned position;
    uint64_t reads, writes, unknown_reads, malformed_frames;
    bool lens_release_input_high;
    uint64_t lens_release_input_reads;
};

static void s5_rtc_reset(DeviceState *dev)
{
    S5RTCPeer *s = S5_RTC_PEER(dev);
    unsigned reg;
    memset(s->registers, 0, sizeof(s->registers));
    memset(s->defined, 0, sizeof(s->defined));
    memset(s->unsupported, 0, sizeof(s->unsupported));
    for (reg = 0x0402; reg <= 0x0410; reg += 2) {
        s->defined[reg >> 1] = true;
    }
    for (reg = 0x0420; reg <= 0x043e; reg += 2) {
        s->defined[reg >> 1] = true;
    }
    s->defined[0x0440 >> 1] = true;
    s->registers[0x040e >> 1] = 1;
    s->command = s->position = 0;
    s->reads = s->writes = s->unknown_reads = s->malformed_frames = 0;
    s->lens_release_input_reads = 0;
    SSI_PERIPHERAL(s)->cs = false;
}

static int s5_rtc_set_cs(SSIPeripheral *dev, bool selected)
{
    S5RTCPeer *s = S5_RTC_PEER(dev);
    if (!selected && s->position && s->position != 3) {
        s->malformed_frames++;
        qemu_log_mask(LOG_UNIMP, "S5_RTC_UNSUPPORTED frame-length=%u\n",
                      s->position);
    }
    s->command = s->position = 0;
    return 0;
}

static uint32_t s5_rtc_transfer(SSIPeripheral *dev, uint32_t value)
{
    S5RTCPeer *s = S5_RTC_PEER(dev);
    unsigned reg, index;
    uint8_t result = 0xff;

    switch (s->position++) {
    case 0:
        s->command = (value & 0xff) << 8;
        return 0xff;
    case 1:
        s->command |= value & 0xff;
        return 0xff;
    case 2:
        reg = s->command & ~1;
        index = reg >> 1;
        if (s->command & 1) {
            s->reads++;
            if (!s->defined[index]) {
                s->unknown_reads++;
                s->unsupported[index] = true;
                qemu_log_mask(LOG_UNIMP,
                              "S5_RTC_UNSUPPORTED read reg=0x%04x result=0x00\n",
                              reg);
            }
            result = s->registers[index];
            /* Original 0x06549df8 reads 044e into packed status byte3.
             * 0x06544d16..20 inverts that byte's bit0 for an input which,
             * after the original debounce, produces the lens-release warning.
             * This explicit external-input hypothesis affects the wire read
             * only. Preserve the guest's output latch and all other bits.
             * It is not a claim that a lens is present or has acknowledged.
             */
            if (reg == 0x044e && s->lens_release_input_high) {
                result |= 1;
                s->lens_release_input_reads++;
            }
        } else {
            s->writes++;
            s->registers[index] = value & 0xff;
            s->defined[index] = true;
        }
        qemu_log_mask(LOG_UNIMP,
                      "S5_RTC_FRAME %s reg=0x%04x tx=0x%02x rx=0x%02x\n",
                      s->command & 1 ? "read" : "write", reg,
                      value & 0xff, result);
        return result;
    default:
        s->malformed_frames++;
        qemu_log_mask(LOG_UNIMP,
                      "S5_RTC_UNSUPPORTED extra-byte=%u tx=0x%02x\n",
                      s->position, value & 0xff);
        return 0xff;
    }
}

static char *s5_rtc_state_json(Object *obj, Error **errp)
{
    S5RTCPeer *s = S5_RTC_PEER(obj);
    GString *json = g_string_new(NULL);
    bool comma = false;
    unsigned index;

    g_string_append_printf(json,
        "{\"profile\":\"assumed-cold-reset\",\"physical_chip_identified\":false,"
        "\"clock_advances\":false,\"reads\":%" PRIu64 ",\"writes\":%" PRIu64
        ",\"unknown_reads\":%" PRIu64 ",\"malformed_frames\":%" PRIu64
        ",\"lens_release_input\":{\"enabled\":%s,"
        "\"scope\":\"explicit external input: read044e bit0 high; not lens presence or peer ACK\","
        "\"register\":\"0x044e\",\"read_mask\":1,"
        "\"stored_value\":%u,\"effective_value\":%u,"
        "\"affected_reads\":%" PRIu64 ",\"stored_value_modified\":false},"
        "\"unsupported_registers\":[", s->reads, s->writes,
        s->unknown_reads, s->malformed_frames,
        s->lens_release_input_high ? "true" : "false",
        s->registers[0x044e >> 1],
        s->registers[0x044e >> 1] | (s->lens_release_input_high ? 1 : 0),
        s->lens_release_input_reads);
    for (index = 0; index < ARRAY_SIZE(s->registers); index++) {
        if (s->unsupported[index]) {
            g_string_append_printf(json, "%s\"0x%04x\"", comma ? "," : "",
                                   index << 1);
            comma = true;
        }
    }
    g_string_append(json, "],\"registers\":{");
    comma = false;
    for (index = 0; index < ARRAY_SIZE(s->registers); index++) {
        if (s->defined[index]) {
            g_string_append_printf(json, "%s\"0x%04x\":%u", comma ? "," : "",
                                   index << 1, s->registers[index]);
            comma = true;
        }
    }
    g_string_append(json, "}}");
    return g_string_free(json, false);
}

static void s5_rtc_realize(SSIPeripheral *dev, Error **errp)
{
    s5_rtc_reset(DEVICE(dev));
}

static bool s5_rtc_get_lens_release_input(Object *obj, Error **errp)
{
    return S5_RTC_PEER(obj)->lens_release_input_high;
}

static void s5_rtc_set_lens_release_input(Object *obj, bool value, Error **errp)
{
    S5_RTC_PEER(obj)->lens_release_input_high = value;
}

static void s5_rtc_class_init(ObjectClass *klass, void *data)
{
    SSIPeripheralClass *sc = SSI_PERIPHERAL_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);
    sc->realize = s5_rtc_realize;
    sc->transfer = s5_rtc_transfer;
    sc->set_cs = s5_rtc_set_cs;
    /* Internal logical selection; not a claim about physical pin polarity. */
    sc->cs_polarity = SSI_CS_HIGH;
    device_class_set_legacy_reset(dc, s5_rtc_reset);
    object_class_property_add_str(klass, "state-json", s5_rtc_state_json, NULL);
    object_class_property_add_bool(klass, "lens-release-input-high",
                                   s5_rtc_get_lens_release_input,
                                   s5_rtc_set_lens_release_input);
    dc->desc = "S5 experimental three-byte RTC/backup register protocol peer";
}

static const TypeInfo s5_rtc_type = {
    .name = TYPE_S5_RTC_PEER,
    .parent = TYPE_SSI_PERIPHERAL,
    .instance_size = sizeof(S5RTCPeer),
    .class_init = s5_rtc_class_init,
};

static void s5_rtc_register_types(void)
{
    type_register_static(&s5_rtc_type);
}

type_init(s5_rtc_register_types)
