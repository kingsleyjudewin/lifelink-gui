import ujson as json

import urequests

import config


def _headers():
    return {
        "Content-Type": "application/json",
        # Backend doesn't enforce this yet (build-order step 8), but send it
        # now so turning on auth later is a backend-only change.
        "X-API-Key": config.API_KEY,
    }


def _request(method, path, payload=None):
    url = config.BACKEND_BASE_URL + path
    try:
        if method == "POST":
            res = urequests.post(url, data=json.dumps(payload), headers=_headers(), timeout=10)
        else:
            res = urequests.get(url, headers=_headers(), timeout=10)
    except OSError as e:
        print(">> backend request failed:", e)
        return None

    try:
        if res.status_code == 204:
            return None
        if res.status_code >= 400:
            print(">> backend error", res.status_code, res.text)
            return None
        return res.json()
    except (OSError, ValueError) as e:
        print(">> bad backend response:", e)
        return None
    finally:
        res.close()


def post(path, payload):
    return _request("POST", path, payload)


def get(path):
    return _request("GET", path)


def poll_pending_command(device_id):
    """Backend-triggered mode selection: atomically claims (and returns) the
    oldest queued command for this device, or None if there isn't one.
    """
    return get("/api/devices/%s/commands/pending" % device_id)


def post_command_result(device_id, command_id, payload):
    return post("/api/devices/%s/commands/%s/result" % (device_id, command_id), payload)


def post_command_progress(device_id, command_id, progress):
    return post("/api/devices/%s/commands/%s/progress" % (device_id, command_id), progress)


def get_patient_templates(patient_id):
    """Decrypted templates from the local Postgres, for the on-sensor
    SetTemplate + Verify1_1 comparison."""
    return get("/api/patients/%s/templates" % patient_id)
