#!/usr/bin/env python3
"""Small, stdlib-only QEMU ARM adapter using local Unix QMP and GDB sockets.

The ``none`` machine has one CPU and RAM starting at zero. It does not model
the Panasonic camera. Callers supply code, registers, memory and stop criteria.
Core GDB register numbers are r0..r15=0..15 and CPSR=25. Thumb execution requires
CPSR.T; QEMU deliberately clears bit zero when the PC register is written.
"""
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time


def _connect_unix(path, process, timeout):
    deadline = time.monotonic() + timeout
    while True:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        try:
            sock.connect(str(path))
            return sock
        except (FileNotFoundError, ConnectionRefusedError):
            sock.close()
            if process.poll() is not None:
                raise RuntimeError("QEMU exited during startup; inspect qemu-stderr.log")
            if time.monotonic() >= deadline:
                raise TimeoutError("Timed out connecting to QEMU socket: %s" % path)
            time.sleep(0.02)


class _QMP:
    def __init__(self, sock, timeout):
        self.sock, self.timeout, self.buffer = sock, timeout, bytearray()
        self.sequence = 0
        if "QMP" not in self._message(time.monotonic() + timeout):
            raise RuntimeError("Invalid QMP greeting")
        self.command("qmp_capabilities")

    def _message(self, deadline):
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("QMP response timed out")
            self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QEMU closed QMP")
            self.buffer.extend(chunk)
        line, _, remainder = self.buffer.partition(b"\n")
        self.buffer = bytearray(remainder)
        return json.loads(line)

    def command(self, name, arguments=None, timeout=None):
        self.sequence += 1
        message = {"execute": name, "id": self.sequence}
        if arguments is not None:
            message["arguments"] = arguments
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self.sock.settimeout(max(0.01, deadline - time.monotonic()))
        self.sock.sendall(json.dumps(message).encode() + b"\n")
        while True:
            result = self._message(deadline)
            if result.get("id") != self.sequence:
                continue  # Asynchronous STOP/RESUME events.
            if "error" in result:
                raise RuntimeError("QMP error: %s" % result["error"])
            return result["return"]

    def close(self):
        self.sock.close()


class _RSP:
    def __init__(self, sock, timeout):
        self.sock, self.timeout, self.buffer = sock, timeout, bytearray()
        self.console_output = bytearray()
        self.packet_size = 4096
        self.features = self.request("qSupported:xmlRegisters=arm")
        match = re.search(r"(?:^|;)PacketSize=([0-9a-fA-F]+)", self.features)
        if match:
            self.packet_size = int(match.group(1), 16)
        # This tells QEMU that this debugger understands target XML. Core
        # register numbers remain the explicit ARM numbers, including CPSR=25.
        self.target_xml = self.read_feature("target.xml")
        self.core_xml = self.read_feature("arm-core.xml")
        self.request("Hg0")
        self.request("Hc0")
        self.initial_stop = self.request("?")

    def _byte(self, deadline):
        if not self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("GDB RSP response timed out")
            self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("QEMU closed GDB RSP")
            self.buffer.extend(chunk)
        value = self.buffer[0]
        del self.buffer[0]
        return value

    def _packet(self, deadline):
        while True:
            value = self._byte(deadline)
            if value == ord("-"):
                raise RuntimeError("QEMU rejected the RSP packet checksum")
            if value == ord("$"):
                break
        wire = bytearray()
        while True:
            value = self._byte(deadline)
            if value == ord("#"):
                break
            wire.append(value)
        checksum = bytes((self._byte(deadline), self._byte(deadline)))
        if sum(wire) % 256 != int(checksum, 16):
            self.sock.sendall(b"-")
            raise RuntimeError("Invalid RSP response checksum")
        self.sock.sendall(b"+")
        decoded, i = bytearray(), 0
        while i < len(wire):
            value = wire[i]
            if value == ord("}"):
                i += 1
                if i == len(wire):
                    raise RuntimeError("Truncated RSP escape")
                decoded.append(wire[i] ^ 0x20)
            elif value == ord("*"):
                i += 1
                if i == len(wire) or not decoded or wire[i] < 29:
                    raise RuntimeError("Invalid RSP run-length encoding")
                decoded.extend(bytes((decoded[-1],)) * (wire[i] - 29))
            else:
                decoded.append(value)
            i += 1
        return bytes(decoded)

    def _response(self, deadline):
        while True:
            reply = self._packet(deadline)
            if reply.startswith(b"O") and reply != b"OK":
                self.console_output.extend(bytes.fromhex(reply[1:].decode("ascii")))
                continue
            if re.fullmatch(rb"E[0-9a-fA-F]{2}.*", reply):
                raise RuntimeError("GDB RSP error: " + reply.decode("ascii"))
            return reply.decode("ascii")

    def request(self, command, timeout=None):
        payload = command.encode("ascii")
        if any(c in payload for c in (ord("$"), ord("#"), ord("}"))):
            raise ValueError("This RSP client sends ASCII command/hex data packets only")
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        self.sock.settimeout(max(0.01, deadline - time.monotonic()))
        self.sock.sendall(b"$" + payload + b"#" + ("%02x" % (sum(payload) % 256)).encode())
        return self._response(deadline)

    def interrupt(self, timeout=2):
        self.sock.settimeout(timeout)
        self.sock.sendall(b"\x03")
        return self._response(time.monotonic() + timeout)

    def read_feature(self, name):
        result, offset = [], 0
        while True:
            value = self.request("qXfer:features:read:%s:%x,400" % (name, offset))
            if not value or value[0] not in "ml":
                raise RuntimeError("QEMU did not supply target XML: " + name)
            result.append(value[1:])
            offset += len(value[1:])
            if value[0] == "l":
                return "".join(result)

    def close(self):
        self.sock.close()


class QemuArm:
    """Context-managed, paused QEMU ARM machine with flat RAM at address zero."""

    def __init__(self, qemu_path, output_dir, ram="3G", cpu="cortex-a7", timeout=10, loads=None,
                 machine="none", smp=1, address_ranges=None, log_mask=None):
        self.qemu_path = Path(qemu_path).resolve()
        self.output_dir = Path(output_dir).resolve()
        match = re.fullmatch(r"([1-9][0-9]*)([MG])", ram)
        if not match:
            raise ValueError("RAM must be an integer number of M or G, e.g. '3G'")
        self.ram_bytes = int(match[1]) * (1024 ** (2 if match[2] == "M" else 3))
        if self.ram_bytes > 4 * 1024 ** 3:
            raise ValueError("This ARM32 probe supports at most 4 GiB RAM")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not isinstance(smp, int) or not 1 <= smp <= 4:
            raise ValueError("This probe supports one to four CPU threads")
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", machine):
            raise ValueError("Use a simple QEMU machine name")
        self.machine, self.smp = machine, smp
        self.log_mask = log_mask
        self.address_ranges = address_ranges or [(0, self.ram_bytes)]
        for address, size in self.address_ranges:
            if address < 0 or size <= 0 or address + size > 0x100000000:
                raise ValueError("Invalid ARM32 mapped address range")
        self.selected_cpu = 0
        self.ram, self.cpu, self.timeout = ram, cpu, timeout
        self.process = self.qmp = self.rsp = self.temp = self.stderr = None
        self.command = None
        self.loads = []
        for path, address in loads or []:
            path = Path(path).resolve()
            size = path.stat().st_size
            self._memory_range(address, size)
            if not size:
                raise ValueError("Cannot preload an empty file: " + str(path))
            if any(address < old_address + old_size and old_address < address + size
                   for old_path, old_address, old_size in self.loads):
                raise ValueError("QEMU loader ranges must not overlap; apply small overlays with write_mem")
            self.loads.append((path, address, size))

    def __enter__(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="s5-rsp-", dir="/tmp")
        qmp_path, gdb_path = Path(self.temp.name) / "qmp", Path(self.temp.name) / "gdb"
        self.command = [str(self.qemu_path), "-M", self.machine, "-cpu", self.cpu,
                        "-m", self.ram, "-S", "-accel", "tcg,thread=single",
                        "-display", "none", "-monitor", "none", "-serial", "none",
                        "-no-user-config", "-nodefaults",
                        "-qmp", "unix:%s,server=on,wait=off" % qmp_path,
                        "-gdb", "unix:%s,server=on,wait=off" % gdb_path]
        if self.smp != 1:
            self.command += ["-smp", str(self.smp)]
        if self.log_mask:
            self.command += ["-d", self.log_mask, "-D", str(self.output_dir / "qemu-trace.log")]
        loaded = []
        for path, address, size in self.loads:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            self.command += ["-device", "loader,file=%s,addr=0x%x,force-raw=on" % (
                str(path).replace(",", ",,"), address)]
            loaded.append({"file": str(path), "address": address, "bytes": size,
                           "sha256": digest.hexdigest()})
        (self.output_dir / "invocation.json").write_text(json.dumps({
            "argv": self.command, "ram_base": 0, "ram_bytes": self.ram_bytes,
            "machine": self.machine, "cpu_count": self.smp,
            "mapped_address_ranges": self.address_ranges,
            "loaded_files": loaded,
            "scope": "Flat-memory ARM function probe; no S5 hardware model" if self.machine == "none" else "Partial research machine; see board-specific model limitations"
        }, indent=2) + "\n")
        try:
            self.stderr = (self.output_dir / "qemu-stderr.log").open("wb")
            self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=self.stderr)
            self.qmp = _QMP(_connect_unix(qmp_path, self.process, self.timeout), self.timeout)
            self.rsp = _RSP(_connect_unix(gdb_path, self.process, self.timeout), self.timeout)
            (self.output_dir / "target.xml").write_text(self.rsp.target_xml)
            (self.output_dir / "arm-core.xml").write_text(self.rsp.core_xml)
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self):
        if self.process is not None and self.process.poll() is None:
            if self.qmp:
                try:
                    self.qmp.command("quit", timeout=2)
                except (OSError, RuntimeError, TimeoutError):
                    pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        if self.rsp:
            (self.output_dir / "gdb-console.log").write_bytes(self.rsp.console_output)
            self.rsp.close()
            self.rsp = None
        if self.qmp:
            self.qmp.close()
            self.qmp = None
        if self.stderr:
            self.stderr.close()
            self.stderr = None
        if self.temp:
            self.temp.cleanup()
            self.temp = None

    @staticmethod
    def _register(register):
        if isinstance(register, str):
            aliases = {"sp": 13, "lr": 14, "pc": 15, "cpsr": 25}
            if register in aliases:
                register = aliases[register]
            elif re.fullmatch(r"r(?:[0-9]|1[0-5])", register):
                register = int(register[1:])
            else:
                raise ValueError("Unknown ARM core register: " + register)
        if register not in tuple(range(16)) + (25,):
            raise ValueError("Only ARM r0-r15 and CPSR (25) are supported")
        return register

    def read_reg(self, register):
        value = bytes.fromhex(self.rsp.request("p%x" % self._register(register)))
        if len(value) != 4:
            raise RuntimeError("QEMU returned an unexpected core register width")
        return int.from_bytes(value, "little")

    def write_reg(self, register, value):
        if not 0 <= value <= 0xffffffff:
            raise ValueError("ARM core register values must fit uint32")
        reply = self.rsp.request("P%x=%s" % (self._register(register), value.to_bytes(4, "little").hex()))
        if reply != "OK":
            raise RuntimeError("QEMU did not accept register write: " + reply)

    def _memory_range(self, address, size):
        if size < 0 or not any(base <= address and address + size <= base + length
                              for base, length in self.address_ranges):
            raise ValueError("Requested address range lies outside declared probe mappings")

    def threads(self):
        ids = []
        reply = self.rsp.request("qfThreadInfo")
        while reply.startswith("m"):
            ids.extend(reply[1:].split(","))
            reply = self.rsp.request("qsThreadInfo")
        if reply != "l" or not ids:
            raise RuntimeError("QEMU did not return a complete CPU thread list")
        return ids

    def select_cpu(self, index):
        ids = self.threads()
        if not 0 <= index < len(ids):
            raise ValueError("CPU index outside available GDB threads")
        for prefix in ("Hg", "Hc"):
            if self.rsp.request(prefix + ids[index]) != "OK":
                raise RuntimeError("Cannot select QEMU CPU thread")
        self.selected_cpu = index
        return ids[index]

    def resume_cpu(self, index, timeout=10):
        """Run only the selected CPU; all other CPU threads remain stopped."""
        thread = self.select_cpu(index)
        return self._run("vCont;c:" + thread, timeout)

    def resume_all(self, timeout=10):
        return self._run("vCont;c", timeout)

    def step_cpu(self, index, timeout=10):
        """Step only this CPU; other CPUs must not race its stop reply.

        QEMU's legacy 's' marks one CPU for stepping but calls vm_start(),
        which also resumes the other CPUs. A thread-qualified vCont action
        uses gdb_continue_partial() and leaves all unspecified CPUs stopped.
        With single-thread TCG, CPU0 must have started once before a secondary
        CPU can run in isolation (tcg-accel-ops-rr.c's initial kick-off wait).
        Firmware breakpoint probes already satisfy that condition.
        """
        thread = self.select_cpu(index)
        return self._run("vCont;s:" + thread, timeout)

    def read_mem(self, address, size):
        self._memory_range(address, size)
        result = bytearray()
        chunk_size = max(1, (self.rsp.packet_size - 64) // 2)
        for offset in range(0, size, chunk_size):
            count = min(chunk_size, size - offset)
            chunk = bytes.fromhex(self.rsp.request("m%x,%x" % (address + offset, count)))
            if len(chunk) != count:
                raise RuntimeError("QEMU returned a short memory read")
            result.extend(chunk)
        return bytes(result)

    def write_mem(self, address, data):
        data = bytes(data)
        self._memory_range(address, len(data))
        chunk_size = max(1, (self.rsp.packet_size - 64) // 2)
        for offset in range(0, len(data), chunk_size):
            chunk = data[offset:offset + chunk_size]
            reply = self.rsp.request("M%x,%x:%s" % (address + offset, len(chunk), chunk.hex()))
            if reply != "OK":
                raise RuntimeError("QEMU did not accept memory write: " + reply)

    def breakpoint_add(self, address, kind=2):
        self._memory_range(address, kind)
        if kind not in (2, 4):
            raise ValueError("ARM/Thumb breakpoint kind must be 2 or 4")
        reply = self.rsp.request("Z0,%x,%x" % (address, kind))
        if reply != "OK":
            raise RuntimeError("QEMU did not accept breakpoint: " + reply)

    def breakpoint_remove(self, address, kind=2):
        reply = self.rsp.request("z0,%x,%x" % (address, kind))
        if reply != "OK":
            raise RuntimeError("QEMU did not remove breakpoint: " + reply)

    def _run(self, command, timeout):
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        try:
            reply = self.rsp.request(command, timeout=timeout)
        except (TimeoutError, socket.timeout):
            try:
                self.rsp.interrupt(timeout=2)
            except (OSError, RuntimeError, TimeoutError):
                self.qmp.command("stop", timeout=2)
            raise TimeoutError("Guest execution timed out after %s seconds" % timeout) from None
        if not reply or reply[0] not in "STWX":
            raise RuntimeError("Unexpected GDB stop reply: " + reply)
        return reply

    def resume(self, timeout=10):
        return self._run("c", timeout)

    def step(self, timeout=10):
        return self.step_cpu(self.selected_cpu, timeout)

    def dump_memory(self, address, size, path):
        self._memory_range(address, size)
        path = Path(path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.qmp.command("stop")
        result = self.qmp.command("human-monitor-command", {
            "command-line": "pmemsave 0x%x 0x%x %s" % (
                address, size, json.dumps(str(path), ensure_ascii=False))
        })
        if result.strip() or not path.exists() or path.stat().st_size != size:
            raise RuntimeError("QMP memory dump failed: " + result)
        return path

    def registers(self):
        return {**{"r%d" % i: self.read_reg(i) for i in range(16)},
                "cpsr": self.read_reg("cpsr")}
