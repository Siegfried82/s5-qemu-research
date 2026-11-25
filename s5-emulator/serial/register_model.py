"""Strict exploratory model of the observed channel-3 register protocol.

The register address/read-bit and BCD RTC map are recovered from stock S5 code.
Reset values are explicit cold-start assumptions, not values read from a camera.
Unknown reads fail rather than manufacturing peripheral readiness.
"""


class UnknownRegister(Exception):
    pass


class ColdRtcRegisters:
    def __init__(self):
        self.values = {address: 0 for address in range(0x420, 0x440, 2)}
        # Stock 0x067531ac uses these fields to decide whether to initialize
        # its own default BCD calendar; it writes 2014-01-01 itself.
        self.values.update({0x40e: 1, 0x410: 0, 0x440: 0})
        self.sources = {address: "synthetic cold-start backup/status reset assumption"
                        for address in self.values}
        self.frame = []
        self.events = []

    def begin(self):
        self.frame = []

    def transfer(self, value):
        self.frame.append(value & 0xff)
        if len(self.frame) < 3:
            return 0xff  # Header responses are discarded by the stock caller.
        if len(self.frame) != 3:
            raise UnknownRegister("Only the observed three-byte register frame is modeled")
        command = (self.frame[0] << 8) | self.frame[1]
        address = command & ~1
        if command & 1:
            if address not in self.values:
                raise UnknownRegister("Unmodeled register read 0x%04x" % address)
            value = self.values[address]
            self.events.append({"operation": "read", "address": hex(address),
                                "value": value, "source": self.sources[address]})
            return value
        self.values[address] = value & 0xff
        self.sources[address] = "preceding observed original-firmware write"
        self.events.append({"operation": "write", "address": hex(address),
                            "value": value & 0xff, "source": self.sources[address]})
        return 0xff  # Stock write routine ignores returned bytes.

    def description(self):
        return {"name": "exploratory cold RTC / cleared backup-register state",
                "hardware_part_identified": False,
                "hardware_reset_defaults_verified": False,
                "rtc_ticks_modeled": False,
                "unmodeled_register_reads": "stop with error",
                "reset_assumptions": {"0x0420..0x043e": 0, "0x040e": 1,
                                      "0x0410": 0, "0x0440": 0},
                "protocol_evidence": ["0x06752fb0 read frame", "0x06753036 write frame"],
                "rtc_evidence": ["0x064d060a calendar reads", "0x064d0982 BCD decode",
                                 "0x067531ac stock cold-calendar initialization"]}
