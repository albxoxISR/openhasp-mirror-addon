import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import struct
import threading
import time

import paho.mqtt.client as paho_mqtt
import requests
from flask import Flask, Response, jsonify, render_template, request

OPTIONS_FILE = "/data/options.json"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")

app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)
log = logging.getLogger("mirror")
logging.basicConfig(level=logging.INFO)

DEBOUNCE_SEC = 1.5
_start_time = time.time()


@app.after_request
def _set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    return response

# MQTT state topics that should NOT trigger a screenshot refresh.
# These are informational only — no framebuffer change.
_SKIP_STATE_TOPICS = {"statusupdate", "backlight"}

# MQTT commands that are non-visual (don't change framebuffer).
_SKIP_COMMANDS = {"statusupdate", "reboot", "factoryreset", "setupap",
                  "calibrate", "antiburn", "discovery",
                  "backlight", "idle"}


# ── Config ────────────────────────────────────────────────────────────────────

_opts_cache = {"data": None, "checked": 0}


def load_options():
    now = time.time()
    if _opts_cache["data"] is not None and now - _opts_cache["checked"] < 5.0:
        return _opts_cache["data"]
    try:
        with open(OPTIONS_FILE) as f:
            data = json.load(f)
        _opts_cache["data"] = data
        _opts_cache["checked"] = now
        return data
    except FileNotFoundError:
        return {"plates": [], "refresh_ms": 500}


def plate_by_name(name):
    opts = load_options()
    for p in opts.get("plates", []):
        if p["name"] == name:
            if not _is_valid_plate_ip(p.get("ip", "")):
                log.error("Configured plate %s has invalid IP: %s", name, p.get("ip"))
                return None
            return p
    return _discovered_plates.get(name)


# ── Plate info (resolution from BMP header) ──────────────────────────────────

_plate_info_cache = {}


def get_plate_info(plate):
    name = plate["name"]
    if name in _plate_info_cache:
        return _plate_info_cache[name]

    info = {"name": name, "ip": plate["ip"], "width": 480, "height": 480}

    try:
        resp = requests.get(
            f"http://{plate['ip']}/screenshot?q=0", timeout=4, stream=True,
        )
        header = resp.raw.read(30)
        resp.close()
        if header[:2] == b"BM" and len(header) >= 26:
            w = struct.unpack_from("<i", header, 18)[0]
            h = struct.unpack_from("<i", header, 22)[0]
            info["width"] = abs(w)
            info["height"] = abs(h)
    except Exception as e:
        log.warning("Could not read BMP header from %s: %s", name, e)

    _plate_info_cache[name] = info
    return info


# ── Screenshot conversion ────────────────────────────────────────────────────

def fetch_screenshot_jpeg(plate_ip):
    from PIL import Image

    resp = requests.get(f"http://{plate_ip}/screenshot?q=0", timeout=5)
    resp.raise_for_status()
    img = Image.open(io.BytesIO(resp.content))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=75)
    return buf.getvalue()


# ── Screenshot cache (MQTT-driven, hash-deduped) ────────────────────────────

_screenshots = {}       # {plate_name: jpeg_bytes}
_ss_version = {}        # {plate_name: int}
_ss_hashes = {}         # {plate_name: md5_digest}
_debounce_timers = {}   # {plate_name: Timer}
_update_cond = threading.Condition()  # guards screenshot state + wakes waiters
_plate_online = {}      # {plate_name: {"online": bool, "since": float}}
_refresh_busy = {}      # {plate_name: bool}  — prevents concurrent HTTP fetches
_refresh_pending = {}   # {plate_name: bool}  — retry after current fetch finishes
_PERIODIC_REFRESH_SEC = 30
_active_viewers = {}    # {plate_name: int} — active long-poll connections

# ── Diagnostic counters (exposed via /api/debug) ────────────────────────────
_diag = {
    "mqtt_msgs": 0,             # total MQTT messages received
    "mqtt_msgs_plate": {},      # {plate_name: count}
    "refresh_triggered": {},    # {plate_name: count}  schedule_refresh called
    "refresh_attempted": {},    # {plate_name: count}  refresh_screenshot started
    "refresh_success": {},      # {plate_name: count}  new screenshot (hash differs)
    "refresh_deduped": {},      # {plate_name: count}  hash matched (skipped)
    "refresh_failed": {},       # {plate_name: count}  HTTP error
    "last_refresh_at": {},      # {plate_name: timestamp}
    "last_refresh_result": {},  # {plate_name: "success|deduped|error:..."}
    "mqtt_reconnects": 0,
    "mqtt_setup": "not started",   # tracks start_mqtt() progress
}


def refresh_screenshot(plate_name):
    """Fetch fresh screenshot, skip if visually identical (hash dedup).

    Serialized per plate: only one HTTP fetch runs at a time to avoid
    overwhelming the ESP32 web server. If another event arrives while
    a fetch is running, it sets _refresh_pending so a follow-up fetch
    happens after the current one finishes.
    """
    # Guard: skip if another fetch is already running for this plate
    with _update_cond:
        if _refresh_busy.get(plate_name):
            _refresh_pending[plate_name] = True
            _diag["refresh_skipped_busy"] = _diag.get("refresh_skipped_busy", 0) + 1
            return
        _refresh_busy[plate_name] = True
        _refresh_pending[plate_name] = False

    plate = plate_by_name(plate_name)
    if not plate:
        _refresh_busy[plate_name] = False
        return
    _diag["refresh_attempted"][plate_name] = _diag["refresh_attempted"].get(plate_name, 0) + 1
    _diag["last_refresh_at"][plate_name] = time.time()
    try:
        jpeg = fetch_screenshot_jpeg(plate["ip"])
        h = hashlib.md5(jpeg).digest()
        with _update_cond:
            if _ss_hashes.get(plate_name) == h:
                _diag["refresh_deduped"][plate_name] = _diag["refresh_deduped"].get(plate_name, 0) + 1
                _diag["last_refresh_result"][plate_name] = "deduped"
                return  # no visual change
            _ss_hashes[plate_name] = h
            _screenshots[plate_name] = jpeg
            _ss_version[plate_name] = _ss_version.get(plate_name, 0) + 1
            _update_cond.notify_all()
        _diag["refresh_success"][plate_name] = _diag["refresh_success"].get(plate_name, 0) + 1
        _diag["last_refresh_result"][plate_name] = "success"
        log.warning("Screenshot NEW for %s (v%d, hash=%s)",
                     plate_name, _ss_version.get(plate_name, 0),
                     h.hex()[:8])
    except Exception as e:
        _diag["refresh_failed"][plate_name] = _diag["refresh_failed"].get(plate_name, 0) + 1
        _diag["last_refresh_result"][plate_name] = f"error:{e}"
        log.error("Screenshot refresh FAILED for %s: %s", plate_name, e)
    finally:
        with _update_cond:
            _refresh_busy[plate_name] = False
            pending = _refresh_pending.get(plate_name)
            if pending:
                _refresh_pending[plate_name] = False
        # If events arrived while we were fetching, schedule one more
        # through the debounce path to coalesce rapid events
        if pending:
            schedule_refresh(plate_name)


def schedule_refresh(plate_name):
    """Debounced: wait for MQTT burst to settle, then refresh."""
    _diag["refresh_triggered"][plate_name] = _diag["refresh_triggered"].get(plate_name, 0) + 1
    if plate_name in _debounce_timers:
        _debounce_timers[plate_name].cancel()
    timer = threading.Timer(DEBOUNCE_SEC, refresh_screenshot, args=[plate_name])
    timer.daemon = True
    _debounce_timers[plate_name] = timer
    timer.start()


def _periodic_refresh():
    """Fallback: refresh plates with active viewers every 30s."""
    opts = load_options()
    for p in opts.get("plates", []):
        if _active_viewers.get(p["name"], 0) > 0:
            schedule_refresh(p["name"])
    for name in list(_discovered_plates.keys()):
        if _active_viewers.get(name, 0) > 0:
            schedule_refresh(name)
    t = threading.Timer(_PERIODIC_REFRESH_SEC, _periodic_refresh)
    t.daemon = True
    t.start()


# ── MQTT client ──────────────────────────────────────────────────────────────

_mqtt_client = None
_object_vals = {}       # {plate_name: {"pXbY": val}}
_discovered_plates = {} # {name: {"name": name, "ip": ip}}


def start_mqtt():
    global _mqtt_client

    if not SUPERVISOR_TOKEN:
        _diag["mqtt_setup"] = "FAIL: no SUPERVISOR_TOKEN env var"
        log.warning("No SUPERVISOR_TOKEN — MQTT disabled")
        return

    _diag["mqtt_setup"] = "has SUPERVISOR_TOKEN, querying supervisor..."
    log.info("SUPERVISOR_TOKEN present (%d chars), querying MQTT service...",
             len(SUPERVISOR_TOKEN))

    try:
        r = requests.get(
            "http://supervisor/services/mqtt",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=5,
        )
        r.raise_for_status()
        mqtt_info = r.json().get("data", {})
    except Exception as e:
        _diag["mqtt_setup"] = f"FAIL: supervisor API error: {e}"
        log.error("Failed to get MQTT info: %s", e)
        return

    host = mqtt_info.get("host", "core-mosquitto")
    port = int(mqtt_info.get("port", 1883))
    username = mqtt_info.get("username", "")
    password = mqtt_info.get("password", "")

    _diag["mqtt_setup"] = f"got credentials, connecting to {host}:{port} user={username}..."
    log.info("MQTT credentials: host=%s port=%d user=%s", host, port, username)

    client = paho_mqtt.Client(client_id="openhasp-mirror")
    if username:
        client.username_pw_set(username, password)

    client.on_connect = _on_connect
    client.on_message = _on_message
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    try:
        client.connect(host, port, keepalive=60)
        client.loop_start()
        _mqtt_client = client
        _diag["mqtt_setup"] = f"OK: connected to {host}:{port}"
        log.info("MQTT connecting to %s:%d", host, port)
    except Exception as e:
        _diag["mqtt_setup"] = f"FAIL: connect error: {e}"
        log.error("MQTT connection failed: %s", e)


def _on_connect(client, userdata, flags, rc):
    if rc != 0:
        log.error("MQTT connect failed: rc=%d", rc)
        return
    log.info("MQTT connected")

    # Subscribe to configured plates (state/# and command/# only;
    # LWT is covered by the wildcard hasp/+/LWT below)
    opts = load_options()
    for p in opts.get("plates", []):
        name = p["name"]
        client.subscribe(f"hasp/{name}/state/#")
        client.subscribe(f"hasp/{name}/command/#")
        log.info("Subscribed to hasp/%s/state/# + command/#", name)

    # Auto-discovery + LWT for all plates (covers configured plates too)
    client.subscribe("hasp/+/LWT")
    client.subscribe("hasp/+/state/statusupdate")
    log.info("Listening for auto-discovery on hasp/+/LWT")


def _on_message(client, userdata, msg):
    _diag["mqtt_msgs"] += 1
    parts = msg.topic.split("/")
    if len(parts) < 3:
        return
    plate_name = parts[1]
    _diag["mqtt_msgs_plate"][plate_name] = _diag["mqtt_msgs_plate"].get(plate_name, 0) + 1
    topic_type = parts[2]  # "state", "command", or "LWT"

    # ── Auto-discovery from statusupdate ──
    if (topic_type == "state" and len(parts) >= 4
            and parts[3] == "statusupdate"):
        try:
            payload = json.loads(msg.payload.decode())
            ip = payload.get("ip")
            if ip:
                _handle_discovery(plate_name, ip, client)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    # ── LWT online/offline tracking ──
    if topic_type == "LWT":
        status = msg.payload.decode().strip().lower()
        is_online = (status == "online")
        prev = _plate_online.get(plate_name, {}).get("online")
        _plate_online[plate_name] = {
            "online": is_online,
            "since": time.time(),
        }
        if prev is not None and prev != is_online:
            log.warning("Plate %s went %s", plate_name,
                        "ONLINE" if is_online else "OFFLINE")
        if is_online and plate_by_name(plate_name):
            # Plate just booted — delay to let it finish rendering
            threading.Timer(2.0, refresh_screenshot,
                            args=[plate_name]).start()
        # Wake long-poll waiters so frontend sees status change
        with _update_cond:
            _update_cond.notify_all()
        return

    # Only process known plates beyond this point
    if not plate_by_name(plate_name):
        return

    should_refresh = False

    # ── state/# messages ──
    if topic_type == "state" and len(parts) >= 4:
        obj_ref = parts[3]

        # Skip non-visual state topics (statusupdate, backlight)
        if obj_ref in _SKIP_STATE_TOPICS:
            return

        try:
            payload = json.loads(msg.payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = msg.payload.decode()

        # Track object values (only for dict payloads like {"event":"up","val":1})
        if isinstance(payload, dict) and "val" in payload:
            _object_vals.setdefault(plate_name, {})[obj_ref] = payload["val"]

        # Track current page — handle all possible formats:
        #   bare number "2", JSON number 2, dict {"page":2}, dict {"val":2}
        if obj_ref == "page":
            log.info("MQTT page topic received: %s payload=%r type=%s",
                     plate_name, payload, type(payload).__name__)
            page_num = _parse_page(payload)
            if page_num is not None:
                _current_page[plate_name] = page_num
                log.info("Page update: %s -> page %d", plate_name, page_num)
            else:
                log.warning("Could not parse page from payload: %r", payload)
            if _active_viewers.get(plate_name, 0) > 0:
                should_refresh = True
        # Idle state change — page may return to home shortly after.
        # Delayed refresh to catch the page change.
        elif obj_ref == "idle":
            if _active_viewers.get(plate_name, 0) > 0:
                threading.Timer(2.0, refresh_screenshot,
                                args=[plate_name]).start()
            return
        # User interaction — payload contains "event" key
        elif isinstance(payload, dict) and "event" in payload:
            if _active_viewers.get(plate_name, 0) > 0:
                should_refresh = True

    # ── command/# messages ──
    # Commands are fully ignored for refresh purposes. The 30s periodic
    # fallback catches HA-driven visual updates. This prevents continuous
    # command streams (camera feed, temp updates) from triggering fetches
    # that freeze the ESP32.

    # ── Debounced screenshot refresh (user interaction only) ──
    if should_refresh:
        log.info("Refresh triggered: %s (topic=%s)", plate_name, msg.topic)
        schedule_refresh(plate_name)
    else:
        log.debug("Skipped refresh: %s (topic=%s)", plate_name, msg.topic)


def _on_disconnect(client, userdata, rc):
    _diag["mqtt_reconnects"] += 1
    log.warning("MQTT disconnected (rc=%d), will reconnect", rc)


def _is_valid_plate_ip(ip):
    """Validate that an IP is a plausible local plate address."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.is_private and not addr.is_loopback
    except ValueError:
        return False


# Plate names must be alphanumeric / underscore / hyphen only
_PLATE_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _handle_discovery(plate_name, ip, client):
    """Register a newly discovered plate from MQTT statusupdate."""
    if not _PLATE_NAME_RE.match(plate_name):
        log.warning("Ignoring discovery with invalid name: %r", plate_name)
        return
    if not _is_valid_plate_ip(ip):
        log.warning("Ignoring discovery with non-private IP: %s", ip)
        return

    # Skip if already configured
    opts = load_options()
    for p in opts.get("plates", []):
        if p["name"] == plate_name:
            return

    if plate_name in _discovered_plates:
        _discovered_plates[plate_name]["ip"] = ip
        return

    _discovered_plates[plate_name] = {"name": plate_name, "ip": ip}
    log.info("Auto-discovered plate: %s (%s)", plate_name, ip)

    client.subscribe(f"hasp/{plate_name}/state/#")
    client.subscribe(f"hasp/{plate_name}/command/#")

    def init():
        try:
            fetch_objects(_discovered_plates[plate_name])
        except Exception as e:
            log.warning("Could not fetch objects for discovered %s: %s",
                         plate_name, e)
        refresh_screenshot(plate_name)

    threading.Thread(target=init, daemon=True).start()


# ── MQTT publish (direct client first, Supervisor API fallback) ──────────────

def mqtt_publish(topic, payload):
    """Publish via paho client (fast) or Supervisor API (fallback)."""
    if _mqtt_client and _mqtt_client.is_connected():
        result = _mqtt_client.publish(topic, payload)
        return result.rc == 0

    if not SUPERVISOR_TOKEN:
        return False
    try:
        r = requests.post(
            "http://supervisor/core/api/services/mqtt/publish",
            headers={
                "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
                "Content-Type": "application/json",
            },
            json={"topic": topic, "payload": payload},
            timeout=5,
        )
        return r.status_code < 300
    except Exception as e:
        log.error("MQTT publish failed: %s", e)
        return False


def _parse_page(payload):
    """Extract page number from any MQTT page payload format."""
    try:
        if isinstance(payload, (int, float)):
            page = int(payload)
        elif isinstance(payload, dict):
            # {"page": 2} or {"val": 2}
            v = payload.get("page", payload.get("val"))
            if v is not None:
                page = int(v)
            else:
                return None
        else:
            page = int(str(payload).strip())
        if page < 0 or page > 255:
            log.warning("Page number out of range: %d", page)
            return None
        return page
    except (TypeError, ValueError):
        return None


# ── Object map (parsed from /pages.jsonl) ────────────────────────────────────

# Types clickable by default in openHASP (widgets that accept user input).
# label, img, obj are NOT clickable unless they have explicit "click": 1.
CLICKABLE_TYPES = {
    "btn", "imgbtn", "btnmatrix", "switch", "checkbox",
    "slider", "arc", "dropdown", "roller", "cpicker",
}

_object_maps = {}
_current_page = {}
_page_cache = {}
_page_entity_id = {}


def fetch_objects(plate):
    name = plate["name"]
    ip = plate["ip"]

    try:
        resp = requests.get(f"http://{ip}/pages.jsonl", timeout=5)
        resp.raise_for_status()
    except Exception as e:
        log.error("Failed to fetch pages.jsonl from %s (%s): %s", name, ip, e)
        return {}

    objects = {}
    current_page = 0

    for line in resp.text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        if "page" in obj and "id" not in obj:
            current_page = obj["page"]
            continue

        if "id" not in obj:
            continue

        obj_type = obj.get("obj", "")
        if not obj_type:
            continue  # skip page background (id 0, no obj type)

        # Disabled or explicitly non-clickable
        if obj.get("click") == 0 or obj.get("enabled") == 0:
            continue

        # Only include types that are clickable by default,
        # OR any type with explicit click: 1
        if obj_type not in CLICKABLE_TYPES and obj.get("click") != 1:
            continue

        x = obj.get("x")
        y_pos = obj.get("y")
        w = obj.get("w")
        h = obj.get("h")
        if x is None or y_pos is None or w is None or h is None:
            continue

        page = obj.get("page", current_page)
        if page not in objects:
            objects[page] = []

        objects[page].append({
            "id": obj["id"],
            "type": obj_type,
            "x": x,
            "y": y_pos,
            "w": w,
            "h": h,
            "page": page,
            "min": obj.get("min", 0),
            "max": obj.get("max", 100),
            "toggle": bool(obj.get("toggle")),
        })

    total = sum(len(v) for v in objects.values())
    log.info("Loaded %d interactive objects for %s (%s)", total, name, ip)
    _object_maps[name] = objects
    return objects


def get_current_page(plate_name):
    # Prefer MQTT-tracked page (set exclusively by _on_message)
    if plate_name in _current_page:
        return _current_page[plate_name]

    # Fallback: query HA entity, cached for 2 seconds
    now = time.time()
    cached = _page_cache.get(plate_name)
    if cached and now - cached[0] < 2.0:
        return cached[1]

    page = _query_page_from_ha(plate_name)
    _page_cache[plate_name] = (now, page)
    return page


def _discover_page_entity(plate_name):
    if not SUPERVISOR_TOKEN:
        return None
    try:
        r = requests.get(
            "http://supervisor/core/api/states",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=5,
        )
        if r.status_code == 200:
            for s in r.json():
                eid = s.get("entity_id", "")
                if plate_name in eid and "page" in eid and (
                        eid.startswith("number.") or eid.startswith("sensor.")):
                    log.info("Discovered page entity for %s: %s", plate_name, eid)
                    return eid
    except Exception as e:
        log.warning("Could not discover page entity for %s: %s", plate_name, e)
    return None


def _query_page_from_ha(plate_name):
    if not SUPERVISOR_TOKEN:
        return 1
    if plate_name not in _page_entity_id:
        _page_entity_id[plate_name] = _discover_page_entity(plate_name)
    entity_id = _page_entity_id.get(plate_name)
    if not entity_id:
        return 1
    try:
        r = requests.get(
            f"http://supervisor/core/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
            timeout=2,
        )
        if r.status_code == 200:
            state = r.json().get("state", "1")
            if state not in ("unknown", "unavailable"):
                page = int(float(state))
                return page
    except Exception as e:
        log.debug("Could not query page for %s: %s", plate_name, e)
    return 1


def find_object_at(plate_name, x, y):
    objects = _object_maps.get(plate_name, {})
    if not objects:
        return None

    page = get_current_page(plate_name)

    # Collect all matching objects, then return the topmost one.
    # In openHASP, later objects in jsonl are rendered on top,
    # and current-page objects sit above page-0 overlay.
    hit = None
    for p in [0, page]:
        for obj in objects.get(p, []):
            if (obj["x"] <= x < obj["x"] + obj["w"] and
                    obj["y"] <= y < obj["y"] + obj["h"]):
                hit = obj  # keep overwriting → last match wins (topmost)
    return hit


# ── Routes ────────────────────────────────────────────────────────────────────

APP_VERSION = "1.0.0"


@app.route("/")
def index():
    ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    return render_template("index.html", base_path=ingress_path,
                           version=APP_VERSION)


@app.route("/api/plates")
def api_plates():
    opts = load_options()
    plates = []
    seen = set()
    for p in opts.get("plates", []):
        plates.append(get_plate_info(p))
        seen.add(p["name"])
    for name, p in _discovered_plates.items():
        if name not in seen:
            info = get_plate_info(p)
            info["discovered"] = True
            plates.append(info)
    return jsonify(plates)


@app.route("/api/wait/<name>")
def api_wait(name):
    """Long-poll: blocks until screenshot version changes or timeout."""
    # Track active viewers — first viewer gets immediate refresh
    is_first = _active_viewers.get(name, 0) == 0
    _active_viewers[name] = _active_viewers.get(name, 0) + 1
    if is_first:
        schedule_refresh(name)
    try:
        v = int(request.args.get("v", 0))
        deadline = time.time() + 25

        while True:
            with _update_cond:
                current_v = _ss_version.get(name, 0)
                if current_v > v:
                    page = get_current_page(name)
                    status = _plate_online.get(name)
                    return jsonify({"v": current_v, "page": page,
                                    "online": status["online"] if status else True})
                remaining = deadline - time.time()
                if remaining <= 0:
                    page = get_current_page(name)
                    status = _plate_online.get(name)
                    return jsonify({"v": current_v, "page": page,
                                    "online": status["online"] if status else True})
                _update_cond.wait(timeout=remaining)
    finally:
        _active_viewers[name] = max(0, _active_viewers.get(name, 0) - 1)


@app.route("/api/screenshot/<name>")
def api_screenshot(name):
    """Return cached screenshot."""
    with _update_cond:
        jpeg = _screenshots.get(name)
        version = _ss_version.get(name, 0)

    if not jpeg:
        plate = plate_by_name(name)
        if not plate:
            return "Plate not found", 404
        try:
            jpeg = fetch_screenshot_jpeg(plate["ip"])
            with _update_cond:
                _screenshots[name] = jpeg
                _ss_version[name] = 1
                version = 1
                _update_cond.notify_all()
        except Exception as e:
            log.error("Screenshot failed for %s: %s", name, e)
            return "Screenshot unavailable", 502

    etag = f'"{name}-{version}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304)

    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@app.route("/api/touch/<name>", methods=["POST"])
def api_touch(name):
    plate = plate_by_name(name)
    if not plate:
        return "Plate not found", 404

    data = request.get_json(force=True)
    try:
        x = int(data.get("x", 0))
        y = int(data.get("y", 0))
        state = int(data.get("state", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid coordinates"}), 400

    info = get_plate_info(plate)
    x = max(0, min(x, info["width"] - 1))
    y = max(0, min(y, info["height"] - 1))

    if state != 0:
        return jsonify({"ok": True, "action": "press"})

    if name not in _object_maps:
        fetch_objects(plate)

    page = get_current_page(name)
    obj = find_object_at(name, x, y)
    objects = _object_maps.get(name, {})
    page_objs = len(objects.get(page, []))
    p0_objs = len(objects.get(0, []))

    if not obj:
        log.warning("Touch %s @ (%d,%d) page=%d — no object (p0:%d pg:%d objs)",
                     name, x, y, page, p0_objs, page_objs)
        return jsonify({"ok": True, "action": "no_object", "page": page,
                        "p0_objs": p0_objs, "page_objs": page_objs})

    obj_page = obj["page"]
    obj_id = obj["id"]
    state_topic = f"hasp/{name}/state/p{obj_page}b{obj_id}"
    cmd_base = f"hasp/{name}/command/p{obj_page}b{obj_id}"

    # Use compact JSON (no spaces) to match firmware output format exactly.
    # HA automations with exact payload matching require this.
    def _js(obj):
        return json.dumps(obj, separators=(",", ":"))

    if obj["type"] == "slider":
        rel = max(0.0, min(1.0, (x - obj["x"]) / max(obj["w"], 1)))
        val = int(obj["min"] + rel * (obj["max"] - obj["min"]))
        mqtt_publish(state_topic, _js({"val": val}))
        mqtt_publish(f"{cmd_base}.val", str(val))
        action = f"slider:{val}"

    elif obj["type"] == "arc":
        rel = max(0.0, min(1.0, 1.0 - (y - obj["y"]) / max(obj["h"], 1)))
        val = int(obj["min"] + rel * (obj["max"] - obj["min"]))
        mqtt_publish(state_topic, _js({"val": val}))
        mqtt_publish(f"{cmd_base}.val", str(val))
        action = f"arc:{val}"

    elif obj["toggle"]:
        obj_ref = f"p{obj_page}b{obj_id}"
        cur_val = _object_vals.get(name, {}).get(obj_ref, 0)
        new_val = 0 if cur_val else 1
        mqtt_publish(f"{cmd_base}.val", str(new_val))
        mqtt_publish(state_topic, _js({"event": "up", "val": new_val}))
        action = f"toggle:{new_val}"

    else:
        # Generic tap — works for btn, obj, label, imgbtn, switch, etc.
        mqtt_publish(state_topic, _js({"event": "down", "val": 1}))
        mqtt_publish(state_topic, _js({"event": "up", "val": 1}))
        mqtt_publish(f"{cmd_base}.val", "1")
        mqtt_publish(f"{cmd_base}.val", "0")
        action = "tap"

    log.info("Touch %s -> p%db%d (%s) [page=%d]", name, obj_page, obj_id, action, page)

    # Force a delayed screenshot refresh — plate needs time to render the change
    threading.Timer(0.4, refresh_screenshot, args=[name]).start()

    return jsonify({"ok": True, "action": action, "object": f"p{obj_page}b{obj_id}",
                    "page": page})


@app.route("/api/page/<name>", methods=["POST"])
def api_page_change(name):
    """Swipe gesture → page next/prev."""
    plate = plate_by_name(name)
    if not plate:
        return "Plate not found", 404
    data = request.get_json(force=True)
    direction = data.get("dir", "next")
    if direction not in ("next", "prev"):
        return jsonify({"ok": False, "error": "invalid direction"}), 400
    mqtt_publish(f"hasp/{name}/command", f"page {direction}")
    threading.Timer(0.4, refresh_screenshot, args=[name]).start()
    return jsonify({"ok": True})


@app.route("/api/objects/<name>")
def api_objects(name):
    plate = plate_by_name(name)
    if not plate:
        return "Plate not found", 404
    if name not in _object_maps:
        fetch_objects(plate)
    objects = _object_maps.get(name, {})
    page = get_current_page(name)
    str_objects = {str(k): v for k, v in objects.items()}
    return jsonify({"page": page, "objects": str_objects})


@app.route("/api/objects/<name>/refresh", methods=["POST"])
def api_refresh_objects(name):
    plate = plate_by_name(name)
    if not plate:
        return "Plate not found", 404
    _plate_info_cache.pop(name, None)
    objects = fetch_objects(plate)
    total = sum(len(v) for v in objects.values())
    return jsonify({"ok": True, "total_objects": total})


@app.route("/api/debug/<name>")
def api_debug(name):
    """Debug endpoint: shows all internal state + diagnostics for a plate."""
    plate = plate_by_name(name)
    mqtt_ok = _mqtt_client.is_connected() if _mqtt_client else False
    objects = _object_maps.get(name, {})
    obj_summary = {str(k): len(v) for k, v in objects.items()}

    status = _plate_online.get(name, {})
    now = time.time()
    last_refresh = _diag["last_refresh_at"].get(name)

    # Sanitize mqtt_setup: only expose OK/FAIL, not host/user/error details
    mqtt_setup_raw = _diag["mqtt_setup"]
    if mqtt_setup_raw.startswith("OK"):
        mqtt_setup_safe = "OK"
    elif mqtt_setup_raw.startswith("FAIL"):
        mqtt_setup_safe = "FAIL"
    else:
        mqtt_setup_safe = "in progress"

    return jsonify({
        "plate_found": plate is not None,
        "plate_source": "config" if plate and name not in _discovered_plates else "discovered" if plate else "none",
        "mqtt_connected": mqtt_ok,
        "mqtt_setup": mqtt_setup_safe,
        "mqtt_reconnects": _diag["mqtt_reconnects"],
        "plate_online": status.get("online", "unknown"),
        "current_page": _current_page.get(name, "NOT SET"),
        "object_map_pages": obj_summary,
        "object_map_total": sum(len(v) for v in objects.values()),
        "ss_version": _ss_version.get(name, 0),
        # ── Diagnostics ──
        "diag_mqtt_msgs_total": _diag["mqtt_msgs"],
        "diag_mqtt_msgs_this_plate": _diag["mqtt_msgs_plate"].get(name, 0),
        "diag_refresh_triggered": _diag["refresh_triggered"].get(name, 0),
        "diag_refresh_attempted": _diag["refresh_attempted"].get(name, 0),
        "diag_refresh_success": _diag["refresh_success"].get(name, 0),
        "diag_refresh_deduped": _diag["refresh_deduped"].get(name, 0),
        "diag_refresh_failed": _diag["refresh_failed"].get(name, 0),
        "diag_refresh_skipped_busy": _diag.get("refresh_skipped_busy", 0),
        "diag_last_refresh_ago_sec": round(now - last_refresh, 1) if last_refresh else None,
        "diag_last_refresh_result": _diag["last_refresh_result"].get(name),
        "diag_uptime_sec": round(now - _start_time, 1),
        "addon_version": APP_VERSION,
    })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    opts = load_options()

    def _init_plates():
        """Pre-fetch objects and initial screenshots in background."""
        for p in opts.get("plates", []):
            try:
                fetch_objects(p)
            except Exception as e:
                log.warning("Could not pre-fetch objects for %s: %s", p["name"], e)
            try:
                refresh_screenshot(p["name"])
            except Exception as e:
                log.warning("Could not fetch initial screenshot for %s: %s", p["name"], e)

    threading.Thread(target=_init_plates, daemon=True).start()
    start_mqtt()
    _periodic_refresh()

    log.info(
        "openHASP Mirror starting — %d plate(s)",
        len(opts.get("plates", [])),
    )
    app.run(host="0.0.0.0", port=8100, threaded=True, use_reloader=False)
