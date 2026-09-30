import network
import ujson as json
import usocket as socket

import urequests
import utime

import config

# ------------------------------------------------------------------ backend discovery
# The backend's IP changes with every WiFi network, so it isn't hard-coded:
# we broadcast LIFELINK_DISCOVER on the LAN and the backend (backend/app.py,
# run_discovery_responder) answers "LIFELINK_BACKEND <port>"; its IP is the
# reply's source address. The last good URL is cached in flash for fast boots.
_CACHE_FILE = "backend_url.txt"
_base_url = None
_failures = 0
REDISCOVER_AFTER_FAILURES = 3


def _load_cached_url():
    try:
        with open(_CACHE_FILE) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _save_cached_url(url):
    if url == _load_cached_url():
        return
    try:
        with open(_CACHE_FILE, "w") as f:
            f.write(url)
    except OSError as e:
        print(">> could not cache backend URL:", e)


def _subnet_broadcast():
    """e.g. 192.168.1.255 for 192.168.1.23/24 (some APs drop 255.255.255.255)."""
    try:
        ip, mask = network.WLAN(network.STA_IF).ifconfig()[:2]
        ip_b = [int(x) for x in ip.split(".")]
        mask_b = [int(x) for x in mask.split(".")]
        return ".".join(str(i | (~m & 0xFF)) for i, m in zip(ip_b, mask_b))
    except (OSError, ValueError):
        return None


def discover_backend(attempts=3):
    """Broadcast for the backend; on success cache and return its base URL."""
    global _base_url, _failures
    port = getattr(config, "DISCOVERY_PORT", 37020)
    targets = ["255.255.255.255"]
    subnet = _subnet_broadcast()
    if subnet and subnet not in targets:
        targets.append(subnet)

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        try:
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BROADCAST", 0x20), 1)
        except OSError:
            pass
        s.settimeout(1.5)
        for _ in range(attempts):
            for target in targets:
                try:
                    s.sendto(b"LIFELINK_DISCOVER", (target, port))
                except OSError:
                    pass
            try:
                data, addr = s.recvfrom(64)
            except OSError:
                continue  # timed out, broadcast again
            parts = data.decode().split()
            if len(parts) == 2 and parts[0] == "LIFELINK_BACKEND":
                url = "http://%s:%s" % (addr[0], parts[1])
                if url != _base_url:
                    print(">> backend discovered at", url)
                _base_url = url
                _failures = 0
                _save_cached_url(url)
                return url
    finally:
        s.close()
    print(">> backend discovery: no answer on UDP %d (backend down, or firewall blocks it)" % port)
    return None


def base_url():
    """Current backend URL: discovered > cached in flash > config fallback."""
    global _base_url
    if not _base_url:
        _base_url = _load_cached_url() or getattr(config, "BACKEND_BASE_URL", None)
    return _base_url


def _headers():
    return {
        "Content-Type": "application/json",
        # The backend refuses device routes without this device's key
        # (DEVICE_API_KEYS in backend/.env).
        "X-API-Key": config.API_KEY,
    }


def _request(method, path, payload=None):
    global _failures
    base = base_url() or discover_backend()
    if not base:
        return None
    url = base + path
    try:
        if method == "POST":
            res = urequests.post(url, data=json.dumps(payload), headers=_headers(), timeout=10)
        else:
            res = urequests.get(url, headers=_headers(), timeout=10)
    except OSError as e:
        print(">> backend request failed:", e)
        _failures += 1
        # Backend moved (new network / new IP) or restarted elsewhere: look again.
        if _failures >= REDISCOVER_AFTER_FAILURES:
            _failures = 0
            discover_backend()
        return None
    _failures = 0

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


def _post_status(path, payload):
    """POST that returns the HTTP status (None if the backend wasn't reached)."""
    base = base_url() or discover_backend()
    if not base:
        return None
    try:
        res = urequests.post(base + path, data=json.dumps(payload), headers=_headers(), timeout=10)
    except OSError as e:
        print(">> backend request failed:", e)
        return None
    try:
        return res.status_code
    finally:
        res.close()


def post_command_result(device_id, command_id, payload, attempts=6):
    """The GUI is waiting on this result, so a WiFi blip must not lose it:
    retry with backoff. 409 = the request was cancelled/expired meanwhile,
    so the result is dropped. Returns True once the backend has it."""
    path = "/api/devices/%s/commands/%s/result" % (device_id, command_id)
    delay_ms = 500
    for attempt in range(attempts):
        status = _post_status(path, payload)
        if status is not None and status < 300:
            return True
        if status in (400, 401, 403, 404, 409):
            print(">> result not accepted (HTTP %s), dropping it" % status)
            return False
        if attempt == 2:
            discover_backend()  # backend may have moved
        utime.sleep_ms(delay_ms)
        delay_ms = min(delay_ms * 2, 4000)
    return False


def get_fingerprint_candidates(finger, offset, limit):
    """A page of stored templates for the 1:N search:
    {total, offset, items: [{patient_id, finger_position, template_data}]}."""
    query = "offset=%d&limit=%d" % (offset, limit)
    if finger:
        query += "&finger=" + finger
    return get("/api/fingerprint/candidates?" + query)


def post_command_progress(device_id, command_id, progress):
    return post("/api/devices/%s/commands/%s/progress" % (device_id, command_id), progress)


def get_patient_templates(patient_id):
    """Decrypted templates from the local Postgres, for the on-sensor
    SetTemplate + Verify1_1 comparison."""
    return get("/api/patients/%s/templates" % patient_id)
