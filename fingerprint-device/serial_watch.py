#!/usr/bin/env python3
"""Passively read and print whatever the ESP32 prints over USB serial,
without sending anything (so it never interrupts main.py's running loop).
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
BAUD = int(sys.argv[2]) if len(sys.argv) > 2 else 115200
DURATION = float(sys.argv[3]) if len(sys.argv) > 3 else 45

ser = serial.Serial(PORT, baudrate=BAUD, timeout=1)
deadline = time.time() + DURATION
while time.time() < deadline:
    line = ser.readline()
    if line:
        try:
            print(line.decode(errors="replace").rstrip())
        except Exception:
            print(repr(line))
ser.close()
