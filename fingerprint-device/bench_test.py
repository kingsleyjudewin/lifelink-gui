#!/usr/bin/env python3
"""Direct USB bench test for the GT-521Fx2 sensor — no ESP32, no backend,
just this PC talking to the sensor over serial. Confirms wiring/comms work
before trusting the full app integration.

Usage: .venv/bin/python bench_test.py [port] [baud]
"""
import sys
import time

from fplib import fplib

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
WAIT_SECONDS = 20


def main():
    print(f">> Connecting to {PORT} @ {BAUD} baud...")
    fp = fplib(PORT, BAUD, timeout=3)
    if not fp.init():
        print(">> FAILED: sensor did not respond to Open. Check wiring/power.")
        return 1
    fp.open()
    print(">> Sensor connected.")

    count = fp.get_enrolled_cnt()
    print(f">> Enrolled templates already on sensor: {count}")

    print(f">> PLACE YOUR FINGER ON THE SENSOR NOW (waiting up to {WAIT_SECONDS}s)...")
    deadline = time.time() + WAIT_SECONDS
    pressed = False
    while time.time() < deadline:
        pressed = fp.is_finger_pressed()
        if pressed:
            print(">> Finger detected!")
            break
        print(".", end="", flush=True)
        time.sleep(1)

    if not pressed:
        print("\n>> TIMED OUT waiting for a finger. No finger was detected.")
        fp.close()
        return 1

    print(">> Capturing template...")
    data, ok = fp.MakeTemplate()
    fp.close()

    if not ok or not data:
        print(">> FAILED to build a template from the capture.")
        return 1

    print(f">> SUCCESS: captured a {len(bytearray(data))}-byte template.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
