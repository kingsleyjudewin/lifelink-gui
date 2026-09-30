# Copy this file to config.py on the device and fill in real values.
# config.py is gitignored -- never commit WiFi credentials or API keys.

# Known WiFi networks, tried strongest-first among the ones in range -- add a
# network here once and the device joins it without editing anything else.
# SSIDs are case-sensitive (and may contain trailing spaces).
WIFI_NETWORKS = [
    ("your-wifi-ssid", "your-wifi-password"),
    ("your-phone-hotspot", "hotspot-password"),
]

# The backend's address is found automatically: the device broadcasts on
# DISCOVERY_PORT and the backend (backend/app.py) replies. The result is cached
# on the device, so a new laptop/Pi IP never needs a reflash.
DISCOVERY_PORT = 37020
# Last-resort fallback only, used if discovery gets no answer.
BACKEND_BASE_URL = "http://192.168.1.50:3000"  # no trailing slash
DEVICE_ID = "dev-esp32-1"
API_KEY = "replace-with-device-api-key"  # must match dev-esp32-1 in DEVICE_API_KEYS (backend/.env)

# UART2 (GPIO16=RX2, GPIO17=TX2 by default on classic ESP32) -- NOT UART0,
# which is the same UART the USB-serial chip uses for the REPL/this console.
# Using UART0 for the sensor fights with mpremote/the REPL over the same pins.
UART_PORT = 2
UART_TX_PIN = 17  # ESP32 TX -> sensor RX
UART_RX_PIN = 16  # ESP32 RX <- sensor TX
UART_BAUD = 115200
SENSOR_LED_PIN = 25

# Templates live only in the local Postgres. During VERIFY each stored
# template is loaded into this one sensor slot, matched, then wiped.
SCRATCH_SLOT = 199
FINGER_TIMEOUT_MS = 20000  # how long to wait for a finger on each scan

BUTTON_PIN = 14
LONG_PRESS_MS = 2000  # hold >= this long for ENROLL, tap for VERIFY
