"""MicroPython driver for the GT-521Fx2 (GT-521F32 / GT-521F52) sensor.

Protocol reference: GT-521F52 datasheet, "UART protocol" section.

  command/response packet (12 bytes):
      55 AA | dev_id(2) | param(4, LE) | cmd/ack(2, LE) | checksum(2, LE)
  data packet (4 + N + 2 bytes):
      5A A5 | dev_id(2) | data(N) | checksum(2, LE)

Every read here is deadline-bounded and checksum-verified -- the previous
driver could spin forever waiting for a reply, and used MicroPython's
default 256-byte UART RX buffer, which silently truncated every 498-byte
template (it's why stored templates were 312 bytes and SetTemplate never
acked: the sensor was still waiting for the rest of the data packet).
"""

import utime
from machine import UART, Pin

CMD_OPEN = 0x01
CMD_CLOSE = 0x02
CMD_CHANGE_BAUD = 0x04
CMD_CMOS_LED = 0x12
CMD_GET_ENROLL_COUNT = 0x20
CMD_CHECK_ENROLLED = 0x21
CMD_ENROLL_START = 0x22
CMD_ENROLL1 = 0x23
CMD_ENROLL2 = 0x24
CMD_ENROLL3 = 0x25
CMD_IS_PRESS_FINGER = 0x26
CMD_DELETE_ID = 0x40
CMD_DELETE_ALL = 0x41
CMD_VERIFY = 0x50
CMD_IDENTIFY = 0x51
CMD_VERIFY_TEMPLATE = 0x52
CMD_CAPTURE_FINGER = 0x60
CMD_MAKE_TEMPLATE = 0x61
CMD_GET_TEMPLATE = 0x70
CMD_SET_TEMPLATE = 0x71

ACK = 0x30
NACK = 0x31

TEMPLATE_SIZE = 498
DEVICE_INFO_SIZE = 24

NACK_ERRORS = {
    0x1001: "timeout",
    0x1002: "invalid_baudrate",
    0x1003: "invalid_pos",
    0x1004: "is_not_used",
    0x1005: "is_already_used",
    0x1006: "comm_err",
    0x1007: "verify_failed",
    0x1008: "identify_failed",
    0x1009: "db_is_full",
    0x100A: "db_is_empty",
    0x100B: "turn_err",
    0x100C: "bad_finger",
    0x100D: "enroll_failed",
    0x100E: "is_not_supported",
    0x100F: "dev_err",
    0x1010: "capture_canceled",
    0x1011: "invalid_param",
    0x1012: "finger_is_not_pressed",
}


class SensorError(Exception):
    pass


class Fingerprint:
    def __init__(self, port=2, baud=115200, tx=17, rx=16, ledpin=None, timeout=3):
        self.port = port
        self.baud = baud
        self.tx = tx
        self.rx = rx
        self.timeout_ms = int(timeout * 1000)
        self.led = Pin(ledpin, Pin.OUT) if ledpin is not None else None
        self.ser = None
        self.last_error = None
        self.device_info = None
        self.led_on = False

    # ------------------------------------------------------------ transport --

    def _open_uart(self, baud):
        if self.ser:
            self.ser.deinit()
        # rxbuf must hold a full response + template data packet (12 + 504).
        self.ser = UART(self.port, baud, tx=self.tx, rx=self.rx, rxbuf=2048, timeout=50)
        self._flush()

    def _flush(self):
        # Bounded: a floating/noisy RX line must not wedge us here forever.
        for _ in range(8):
            if not self.ser.any():
                return
            self.ser.read(self.ser.any())

    @staticmethod
    def _checksum(buf):
        return sum(buf) & 0xFFFF

    def _read_exact(self, n, deadline):
        buf = bytearray()
        while len(buf) < n:
            if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
                return None
            chunk = self.ser.read(n - len(buf))
            if chunk:
                buf.extend(chunk)
        return buf

    def _sync(self, b0, b1, deadline):
        """Skip bytes until the two-byte packet header shows up."""
        prev = None
        while True:
            b = self._read_exact(1, deadline)
            if b is None:
                return False
            if prev == b0 and b[0] == b1:
                return True
            prev = b[0]

    def _send_cmd(self, cmd, param=0):
        pkt = bytearray(12)
        pkt[0] = 0x55
        pkt[1] = 0xAA
        pkt[2] = 0x01
        pkt[3] = 0x00
        param &= 0xFFFFFFFF
        pkt[4] = param & 0xFF
        pkt[5] = (param >> 8) & 0xFF
        pkt[6] = (param >> 16) & 0xFF
        pkt[7] = (param >> 24) & 0xFF
        pkt[8] = cmd & 0xFF
        pkt[9] = (cmd >> 8) & 0xFF
        chk = self._checksum(pkt[:10])
        pkt[10] = chk & 0xFF
        pkt[11] = chk >> 8
        self._flush()
        self.ser.write(pkt)

    def _read_response(self, timeout_ms=None):
        """Returns (ack, param). Raises SensorError on timeout / bad checksum."""
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms or self.timeout_ms)
        if not self._sync(0x55, 0xAA, deadline):
            raise SensorError("no response from sensor")
        rest = self._read_exact(10, deadline)
        if rest is None:
            raise SensorError("truncated response")
        pkt = bytearray(b"\x55\xaa") + rest
        if self._checksum(pkt[:10]) != (pkt[10] | (pkt[11] << 8)):
            raise SensorError("bad response checksum")
        param = pkt[4] | (pkt[5] << 8) | (pkt[6] << 16) | (pkt[7] << 24)
        ack = (pkt[8] | (pkt[9] << 8)) == ACK
        self.last_error = None if ack else NACK_ERRORS.get(param, "nack_0x%04x" % param)
        return ack, param

    def _read_data(self, size, timeout_ms=None):
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms or self.timeout_ms)
        if not self._sync(0x5A, 0xA5, deadline):
            raise SensorError("no data packet from sensor")
        rest = self._read_exact(2 + size + 2, deadline)
        if rest is None:
            raise SensorError("truncated data packet")
        pkt = bytearray(b"\x5a\xa5") + rest
        if self._checksum(pkt[:-2]) != (pkt[-2] | (pkt[-1] << 8)):
            raise SensorError("bad data checksum")
        return bytes(pkt[4:-2])

    def _write_data(self, data):
        pkt = bytearray(b"\x5a\xa5\x01\x00") + data
        chk = self._checksum(pkt)
        pkt.append(chk & 0xFF)
        pkt.append(chk >> 8)
        self.ser.write(pkt)

    def command(self, cmd, param=0, timeout_ms=None):
        if self.led:
            self.led.value(1)
        try:
            self._send_cmd(cmd, param)
            return self._read_response(timeout_ms)
        finally:
            if self.led:
                self.led.value(0)

    # ------------------------------------------------------------- lifecycle --

    def _try_open(self):
        try:
            ack, _ = self.command(CMD_OPEN, 1, timeout_ms=600)
            if ack:
                self.device_info = self._read_data(DEVICE_INFO_SIZE, 600)
            return ack
        except SensorError:
            return False

    def init(self):
        """Connects at self.baud. The sensor powers up at 9600 and keeps
        whatever rate it was last switched to until power is lost, so try
        the target rate first, then fall back to 9600 + ChangeBaudrate.
        Returns True only if the sensor actually ACKs Open."""
        self._open_uart(self.baud)
        if self._try_open():
            return True
        for baud in (9600, 115200, 57600, 38400, 19200):
            if baud == self.baud:
                continue
            self._open_uart(baud)
            if not self._try_open():
                continue
            print(">> sensor answered at %d, switching to %d" % (baud, self.baud))
            try:
                self.command(CMD_CHANGE_BAUD, self.baud, timeout_ms=600)
            except SensorError:
                pass
            utime.sleep_ms(100)
            self._open_uart(self.baud)
            return self._try_open()
        return False

    def open(self):
        return self._try_open()

    def close(self):
        try:
            return self.command(CMD_CLOSE)[0]
        except SensorError:
            return False

    # ---------------------------------------------------------------- finger --

    def set_led(self, on):
        ack = self.command(CMD_CMOS_LED, 1 if on else 0)[0]
        if ack:
            self.led_on = on
        return ack

    def is_finger_pressed(self):
        # With the backlight off this sensor never answers IsPressFinger and
        # wedges until power-cycled, so always light it first.
        if not self.led_on:
            self.set_led(True)
            utime.sleep_ms(300)
        ack, param = self.command(CMD_IS_PRESS_FINGER)
        return ack and param == 0

    def wait_for_finger(self, timeout_ms=20000):
        """Backlight must be on for IsPressFinger to work."""
        self.set_led(True)
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if self.is_finger_pressed():
                return True
            utime.sleep_ms(100)
        self.last_error = "finger_timeout"
        return False

    def wait_for_finger_removed(self, timeout_ms=20000):
        """Flashes the backlight (the operator's "lift your finger" cue)
        until the finger is off the sensor. Needs 3 clear readings in a row:
        IsPressFinger right after the backlight comes back on can misread
        "not pressed" while a finger is still down."""
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        clear = 0
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if not self.is_finger_pressed():
                clear += 1
                if clear >= 3:
                    return True
                utime.sleep_ms(100)
                continue
            clear = 0
            self.set_led(False)
            utime.sleep_ms(150)
            self.set_led(True)
            utime.sleep_ms(300)
        self.last_error = "finger_not_lifted"
        return False

    def capture_finger(self, best=True):
        return self.command(CMD_CAPTURE_FINGER, 1 if best else 0, timeout_ms=5000)[0]

    def capture_when_pressed(self, timeout_ms=20000, attempts=3):
        """Wait for a finger, let it settle, then capture. A finger that
        slides off before the capture just means waiting for it again
        (until timeout_ms); other bad captures are retried `attempts` times.
        Leaves the backlight on."""
        deadline = utime.ticks_add(utime.ticks_ms(), timeout_ms)
        failures = 0
        while True:
            remaining = utime.ticks_diff(deadline, utime.ticks_ms())
            if remaining <= 0 or not self.wait_for_finger(remaining):
                self.last_error = self.last_error or "finger_timeout"
                return False
            # Let the finger settle flat -- capturing on first contact gives a
            # partial print that enrolls/matches poorly.
            utime.sleep_ms(400)
            if self.capture_finger(best=True):
                return True
            if self.last_error != "finger_is_not_pressed":
                failures += 1
                if failures >= attempts:
                    return False
            utime.sleep_ms(200)

    # -------------------------------------------------------------- template --

    def make_template(self):
        """Template (498 bytes) of the last captured image, or None."""
        ack, _ = self.command(CMD_MAKE_TEMPLATE, timeout_ms=5000)
        if not ack:
            return None
        return self._read_data(TEMPLATE_SIZE, 3000)

    def enroll_template(self, scratch_slot, finger_timeout_ms=20000, on_step=None, check_duplicates=()):
        """Three-scan enrollment whose 498-byte template is returned, not
        kept: enrolls into `scratch_slot`, reads it back with GetTemplate,
        then deletes it. ("Enroll without saving", EnrollStart(-1), always
        NACKs Enroll3 with enroll_failed on this sensor's 2017 firmware.)

        check_duplicates: slots the caller loaded with fingers already
        captured; the finger is first 1:1-verified against each, and a hit
        fails with last_error "duplicate_finger" and self.duplicate_slot set.
        (Enroll3 would also reject it, but only as an indistinguishable
        enroll_failed.)

        on_step(stage, scan) is called with ("place", n) / ("lift", n).
        Returns bytes, or None with last_error set."""
        self.duplicate_slot = None
        self.delete(scratch_slot)
        try:
            # A finger left on from the previous digit must not be enrolled
            # as this one.
            if not self.wait_for_finger_removed(finger_timeout_ms):
                return None
            if check_duplicates:
                if on_step:
                    on_step("place", 1)
                if not self.capture_when_pressed(finger_timeout_ms):
                    return None
                # 1:1 against each loaded finger, not Identify1_N: on this
                # sensor Identify1_N falsely matched a different finger 3/3
                # times where Verify1_1 correctly rejected it.
                for slot in check_duplicates:
                    if self.verify_captured(slot):
                        self.duplicate_slot = slot
                        self.last_error = "duplicate_finger"
                        return None
            if not self.command(CMD_ENROLL_START, scratch_slot)[0]:
                return None
            steps = (CMD_ENROLL1, CMD_ENROLL2, CMD_ENROLL3)
            for i, cmd in enumerate(steps):
                if on_step:
                    on_step("place", i + 1)
                if not self.capture_when_pressed(finger_timeout_ms):
                    return None
                ack, _ = self.command(cmd, timeout_ms=8000)
                if not ack:
                    return None
                if cmd == CMD_ENROLL3:
                    return self.get_template(scratch_slot)
                if on_step:
                    on_step("lift", i + 1)
                # Each scan must be a separate placement, or Enroll3 NACKs.
                if not self.wait_for_finger_removed(finger_timeout_ms):
                    return None
        finally:
            err = self.last_error
            self.set_led(False)
            self.delete(scratch_slot)
            self.last_error = err

    def get_template(self, slot):
        ack, _ = self.command(CMD_GET_TEMPLATE, slot)
        if not ack:
            return None
        return self._read_data(TEMPLATE_SIZE, 3000)

    def set_template(self, slot, template):
        if len(template) != TEMPLATE_SIZE:
            self.last_error = "bad_template_size"
            return False
        ack, _ = self.command(CMD_SET_TEMPLATE, slot)
        if not ack:
            return False
        self._write_data(template)
        return self._read_response(5000)[0]

    def verify_captured(self, slot):
        """1:1 match of the last CaptureFinger image against `slot`."""
        return self.command(CMD_VERIFY, slot, timeout_ms=5000)[0]

    def delete(self, slot=None):
        if slot is None:
            return self.command(CMD_DELETE_ALL)[0]
        return self.command(CMD_DELETE_ID, slot)[0]

    def is_enrolled(self, slot):
        return self.command(CMD_CHECK_ENROLLED, slot)[0]

    def get_enrolled_cnt(self):
        ack, param = self.command(CMD_GET_ENROLL_COUNT)
        return param if ack else -1
