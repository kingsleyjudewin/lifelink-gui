"""ENROLL and VERIFY mode logic for the GT-521Fx2 relay.

The sensor is stateless: the local Postgres (via the backend) is the only
place templates are stored. ENROLL runs a 3-scan enrollment into a single
scratch slot, reads the merged 498-byte template back, wipes the slot, and
ships the template to the backend. VERIFY loads each of the claimed
patient's stored templates into a scratch slot (SetTemplate), captures one
live scan, runs the on-chip 1:1 match (Verify1_1) against each slot, and
wipes the scratch slots again afterwards.
"""

import ubinascii

import config
import device_api
from fplibMicro import TEMPLATE_SIZE, SensorError

SCRATCH_SLOT = getattr(config, "SCRATCH_SLOT", 199)
FINGER_TIMEOUT_MS = getattr(config, "FINGER_TIMEOUT_MS", 20000)
ENROLL_ATTEMPTS = 2


def _b64(data):
    return ubinascii.b2a_base64(bytes(data)).decode().strip()


def enroll_mode(fp, exclude_templates=(), on_progress=None):
    """exclude_templates: fingers already captured in this registration.
    They're loaded into scratch slots below SCRATCH_SLOT so the sensor can
    refuse the same finger being enrolled twice, and wiped afterwards."""
    print(">> ENROLL mode -- place finger 3 times")

    def on_step(stage, scan):
        print(">> scan %d/3: %s finger" % (scan, stage))
        if on_progress:
            on_progress({"stage": stage, "scan": scan, "of": 3})

    loaded = []
    try:
        loaded = _load_templates(fp, exclude_templates, SCRATCH_SLOT - 1)
        for attempt in range(ENROLL_ATTEMPTS):
            if attempt:
                # The 3 scans didn't merge (usually the finger was placed at
                # a different spot/angle) -- run the whole finger again.
                print(">> scans did not match, retrying this finger")
                if on_progress:
                    on_progress({"stage": "retry", "scan": 1, "of": 3})
            template = fp.enroll_template(
                SCRATCH_SLOT, FINGER_TIMEOUT_MS, on_step=on_step,
                check_duplicates=[slot for slot, _ in loaded]
            )
            # Read these before _wipe(): every sensor command resets last_error.
            error, duplicate_slot = fp.last_error, fp.duplicate_slot
            if template or error not in ("enroll_failed", "bad_finger"):
                break
    except SensorError as e:
        print(">> sensor error during enroll:", e)
        return {"ok": False, "error": "sensor_error"}
    finally:
        _wipe(fp, loaded)
    if not template:
        print(">> enroll failed:", error)
        result = {"ok": False, "error": error or "capture_failed"}
        if error == "duplicate_finger":
            result["duplicate_of"] = dict(loaded).get(duplicate_slot)
        return result
    print(">> enrolled template: %d bytes (not stored on sensor)" % len(template))
    return {"ok": True, "template_data": _b64(template), "device_slot": None}


def _wipe(fp, loaded):
    try:
        for slot, _ in loaded:
            fp.delete(slot)
    except SensorError:
        pass


def submit_enrollment(patient_id, finger_position, template_data):
    return device_api.post(
        "/api/enroll",
        {
            "patient_id": patient_id,
            "finger_position": finger_position,
            "device_id": config.DEVICE_ID,
            "template_data": template_data,
        },
    )


def _load_templates(fp, templates, first_slot=SCRATCH_SLOT):
    """Loads each template into its own scratch slot (first_slot,
    first_slot-1, ...). Must happen BEFORE the live capture: SetTemplate
    reuses the sensor's image buffer, so loading after capturing makes every
    Verify1_1 fail. Returns [(slot, finger_position)]."""
    loaded = []
    for tpl in templates:
        data = ubinascii.a2b_base64(tpl["template_data"])
        if len(data) != TEMPLATE_SIZE:
            print(">> skipping %s: stored template is %d bytes, expected %d (re-enroll)"
                  % (tpl["finger_position"], len(data), TEMPLATE_SIZE))
            continue
        slot = first_slot - len(loaded)
        fp.delete(slot)
        if fp.set_template(slot, data):
            loaded.append((slot, tpl["finger_position"]))
        else:
            print(">> SetTemplate failed:", fp.last_error)
    return loaded


def verify_mode(fp, patient_id):
    print(">> VERIFY mode, claimed patient_id =", patient_id)

    templates = device_api.get_patient_templates(patient_id)
    if not templates:
        return {"ok": False, "error": "no_stored_templates"}

    loaded = []
    try:
        loaded = _load_templates(fp, templates)
        if not loaded:
            return {"ok": False, "error": "no_usable_templates"}
        # A finger left on from a previous scan must not be taken as this one.
        fp.wait_for_finger_removed(FINGER_TIMEOUT_MS)
        print(">> place finger")
        if not fp.capture_when_pressed(FINGER_TIMEOUT_MS):
            return {"ok": False, "error": fp.last_error or "capture_failed"}
        fp.set_led(False)
        matched_position = None
        for slot, position in loaded:
            if fp.verify_captured(slot):
                matched_position = position
                break
    except SensorError as e:
        print(">> sensor error during verify:", e)
        return {"ok": False, "error": "sensor_error"}
    finally:
        try:
            fp.set_led(False)
        except SensorError:
            pass
        _wipe(fp, loaded)

    match = matched_position is not None
    confidence = 1.0 if match else 0.0
    print(">> verify:", "MATCH (%s)" % matched_position if match else "no match")

    result = device_api.post(
        "/api/verify",
        {
            "device_id": config.DEVICE_ID,
            "patient_id": patient_id,
            "match": match,
            "confidence": confidence,
            "finger_position": matched_position,
        },
    )
    if result is None:
        return {"ok": False, "error": "backend_unreachable"}
    return {"match": match, "confidence": confidence, "finger_position": matched_position}
