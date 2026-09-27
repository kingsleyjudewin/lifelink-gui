import network
import utime
from machine import Pin

import config
import device_api
import modes
from fplibMicro import Fingerprint, SensorError

wlan = network.WLAN(network.STA_IF)


def connect_wifi():
    # A fresh boot can leave the WLAN driver in a state where connect()
    # raises `OSError: Wifi Internal State Error` -- toggling active(False)
    # then True first resets it reliably.
    wlan.active(False)
    utime.sleep_ms(200)
    wlan.active(True)
    utime.sleep_ms(200)

    print(">> connecting to WiFi: %r" % config.WIFI_SSID)
    try:
        wlan.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
    except OSError as e:
        print(">> wlan.connect() failed:", e)
        return False

    deadline = utime.ticks_add(utime.ticks_ms(), 15000)
    while not wlan.isconnected():
        if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
            print(">> WiFi connect timed out")
            return False
        utime.sleep_ms(200)
    print(">> WiFi connected:", wlan.ifconfig())
    return True


def rx_line_floating(pin_no):
    """A powered, connected sensor drives its TX (our RX) high when idle, so
    a pull-down can't pull it low. If it can, nothing is driving the wire."""
    pin = Pin(pin_no, Pin.IN, Pin.PULL_DOWN)
    utime.sleep_ms(5)
    low = sum(1 for _ in range(200) if pin.value() == 0)
    Pin(pin_no, Pin.IN, None)
    return low > 190


def init_sensor():
    fp = Fingerprint(
        port=config.UART_PORT,
        baud=config.UART_BAUD,
        tx=getattr(config, "UART_TX_PIN", 17),
        rx=getattr(config, "UART_RX_PIN", 16),
        ledpin=config.SENSOR_LED_PIN,
        timeout=3,
    )
    while not fp.init():
        print(">> sensor not responding on UART%d (TX=GPIO%d, RX=GPIO%d) -- check wiring/power, retrying"
              % (fp.port, fp.tx, fp.rx))
        if rx_line_floating(fp.rx):
            print(">> GPIO%d is floating: sensor TX wire is not connected, or the sensor has no power/GND" % fp.rx)
        utime.sleep(3)
    fw = fp.device_info[:4] if fp.device_info else b""
    print(">> sensor connected, firmware %s" % "".join("%02x" % b for b in reversed(fw)))
    fp.set_led(False)
    return fp


def read_button_mode(button):
    """Call once button.value() == 0 (pressed). Blocks until release, then
    returns 'enroll' for a long press (>= config.LONG_PRESS_MS) or 'verify'
    for a short press. Assumes a pull-up: idle high, pressed low.
    """
    pressed_at = utime.ticks_ms()
    while button.value() == 0:
        utime.sleep_ms(20)
    held_ms = utime.ticks_diff(utime.ticks_ms(), pressed_at)
    return "enroll" if held_ms >= config.LONG_PRESS_MS else "verify"


def handle_backend_command(fp, command):
    """A command from the frontend, via POST /api/devices/{id}/commands.
    Runs the capture, then reports the outcome back onto the same row so
    the frontend's poll can see it.
    """
    command_id = command["id"]
    mode = command["mode"]
    print(">> backend command #%s: %s" % (command_id, mode))

    if mode == "enroll":
        params = command.get("params") or {}
        capture = modes.enroll_mode(
            fp,
            params.get("exclude_templates") or [],
            on_progress=lambda p: device_api.post_command_progress(config.DEVICE_ID, command_id, p),
        )
        if capture["ok"]:
            payload = {
                "status": "done",
                "result": {"template_data": capture["template_data"], "device_slot": capture["device_slot"]},
            }
        else:
            payload = {"status": "error", "result": {
                "error": capture["error"], "duplicate_of": capture.get("duplicate_of")}}
    else:
        result = modes.verify_mode(fp, command["patient_id"])
        if "match" in result:
            payload = {
                "status": "done",
                "result": {
                    "match": result["match"],
                    "confidence": result["confidence"],
                    "finger_position": result["finger_position"],
                },
            }
        else:
            payload = {"status": "error", "result": {"error": result.get("error", "verify_failed")}}

    device_api.post_command_result(config.DEVICE_ID, command_id, payload)
    summary = dict(payload["result"])
    if "template_data" in summary:
        summary["template_data"] = "<%d b64 chars>" % len(summary["template_data"])
    print(">> reported:", payload["status"], summary)


def run_enroll_standalone(fp):
    """Physical-button fallback with no frontend command involved -- for
    bench testing without the web app. The operator supplies an
    already-existing lifelink_id/finger_position over the serial console.
    """
    capture = modes.enroll_mode(fp)
    if not capture["ok"]:
        print(">> enroll capture failed:", capture["error"])
        return
    patient_id = input("lifelink_id for this enrollment (e.g. LL-1234-M): ").strip()
    finger_position = input("finger_position (e.g. R1): ").strip()
    print(">> enroll result:", modes.submit_enrollment(patient_id, finger_position, capture["template_data"]))


def run_verify_standalone(fp):
    patient_id = input("claimed lifelink_id: ").strip()
    print(">> verify result:", modes.verify_mode(fp, patient_id))


def main():
    connect_wifi()
    fp = init_sensor()
    button = Pin(config.BUTTON_PIN, Pin.IN, Pin.PULL_UP)

    print(">> ready -- polling %s for commands as %s" % (config.BACKEND_BASE_URL, config.DEVICE_ID))
    print(">> button: hold >= %dms for standalone ENROLL, tap for VERIFY" % config.LONG_PRESS_MS)
    while True:
        try:
            if not wlan.isconnected():
                print(">> WiFi lost, reconnecting")
                if not connect_wifi():
                    utime.sleep(5)
                    continue

            command = device_api.poll_pending_command(config.DEVICE_ID)
            if command:
                handle_backend_command(fp, command)
                continue

            if button.value() == 0:
                if read_button_mode(button) == "enroll":
                    run_enroll_standalone(fp)
                else:
                    run_verify_standalone(fp)
                continue
        except SensorError as e:
            # Sensor dropped off the bus (loose wire / brown-out) -- reconnect.
            print(">> sensor error:", e)
            fp = init_sensor()
        except Exception as e:
            print(">> loop error:", repr(e))

        utime.sleep_ms(1000)


if __name__ == "__main__":
    main()
