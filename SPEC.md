# openHASP Mirror — Full Specification

## Goal
Live screenshot mirror + remote touch control for any openHASP plate.
Runs as a Home Assistant addon with ingress sidebar integration.
Must be fully generic — no firmware changes needed.

---

## Architecture

```
[Plate HTTP]                    [MQTT Broker]                [HA API]
  /screenshot ──> JPEG cache      hasp/+/state/# ──> triggers    /api/states ──> page entity
  /pages.jsonl ──> object map     refresh + page tracking          fallback page query
        |                              |                              |
        v                              v                              v
    ┌──────────────────────────────────────────────────────────────────────┐
    │                        Flask Backend (app.py)                       │
    │  - Screenshot cache (hash-deduped, versioned)                      │
    │  - Object map (parsed from pages.jsonl, filtered by CLICKABLE_TYPES)│
    │  - Page tracking (MQTT primary, HA entity fallback with 2s cache)  │
    │  - Touch handler (MQTT publish to state + command topics)          │
    │  - Long-poll endpoint (blocks until screenshot version changes)    │
    └───────┬──────────────────────────────────────────────────────────────┘
            │ HTTP API
            v
    ┌───────────────────────────────────────────┐
    │           Browser Frontend (app.js)        │
    │  - Long-poll loop per plate               │
    │  - Object map for hover highlight         │
    │  - Touch/click/swipe handlers             │
    │  - Page indicator with object count       │
    └───────────────────────────────────────────┘
```

---

## Data Flow: Screenshot Update

1. Something changes on the plate (touch, automation, etc.)
2. Plate publishes MQTT on `hasp/<name>/state/pXbY` (object state change)
3. Addon receives in `_on_message` → calls `schedule_refresh(plate_name)`
4. Debounce timer (150ms) fires → `refresh_screenshot(plate_name)`
5. Fetches BMP from `http://<ip>/screenshot?q=0`, converts to JPEG via Pillow
6. MD5 hash compared to previous — if identical, skip (dedup)
7. If new: store JPEG, bump `_ss_version`, notify all waiters via `_update_cond`
8. Frontend long-poll (`/api/wait/<name>?v=N`) wakes up, returns new version + page
9. Browser fetches new screenshot image via `<img src="/api/screenshot/<name>?v=N">`

**Fallback if MQTT is not connected:**
- Touch handler triggers 0.4s delayed `refresh_screenshot` after each click
- This is the ONLY way screenshots update without MQTT

---

## Data Flow: Page Tracking

**This is the most critical subsystem — gets it wrong and all clicks fail.**

### Primary: MQTT
1. Plate changes page → publishes `hasp/<name>/state/page` with payload (bare int `7`, or `{"page":7}`)
2. `_on_message` receives, `_parse_page()` handles all formats
3. Sets `_current_page[plate_name] = page_num`

### Fallback: HA Entity Query
1. `get_current_page()` checks `_current_page` first (MQTT-set)
2. If NOT set by MQTT: queries HA entity (e.g. `sensor.plate01_plate01_page`)
3. Caches result for 2 seconds to avoid hammering HA API
4. **CRITICAL**: HA query must NOT write to `_current_page` — that dict is reserved for MQTT values only. Otherwise it blocks future HA queries.

### Current Bug (causes "Page 1 (0 obj)")
`_query_page_from_ha()` sets `_current_page[plate_name] = page` as a side effect.
Once set, `get_current_page()` sees `plate_name in _current_page` → True, returns stale value.
If MQTT never updates it (because MQTT client isn't connected or isn't receiving page messages), the page is stuck forever.

**Fix**: Remove the `_current_page` side effect from `_query_page_from_ha`. Keep `_current_page` exclusively for MQTT. Always fall through to HA query (with cache) when MQTT hasn't set it.

---

## Data Flow: Object Detection

### Parsing (`fetch_objects`)
1. Fetch `http://<ip>/pages.jsonl` at startup (and on demand via refresh endpoint)
2. Parse each JSON line:
   - Lines with `page` but no `id` → page header (set current_page context)
   - Lines with `id` → object definition
3. **Filter rules** (openHASP clickability defaults):
   - Skip objects with no `obj` type (page backgrounds, id=0)
   - Skip objects with explicit `click: 0` or `enabled: 0`
   - **Include** only types in `CLICKABLE_TYPES`: `btn, imgbtn, btnmatrix, switch, checkbox, slider, arc, dropdown, roller, cpicker`
   - **Include** any type with explicit `click: 1` (override)
   - **Exclude** `label, img, obj` unless they have explicit `click: 1`
4. Store as `_object_maps[plate_name] = {page_num: [obj_list]}`

### Hit-testing (`find_object_at`)
1. Get current page from `get_current_page()`
2. Check page 0 (overlay) objects, then current page objects
3. **Topmost wins**: iterate all objects, keep overwriting hit → last match wins
   (openHASP renders later jsonl objects on top)
4. Frontend `findObjectAt` must use the SAME algorithm (last match)

### Expected object counts (plate01, verified against live pages.jsonl):
| Page | Clickable | Types |
|------|-----------|-------|
| 0    | 7         | 7 btn (nav buttons) |
| 1    | 0         | all label/obj (display only) |
| 2    | 9         | 9 btn (BACK, toggle cards, ON/OFF, PREV) |
| 3    | 3         | 3 btn (ALARM, LEAK, SMOKE cards) |
| 4    | 0         | label/obj/spinner/img (display only) |
| 5    | 5         | 2 btn + 2 slider + 1 cpicker |
| 6    | 3         | 3 btn (DISARM, ARM, BYPASS) |
| 7    | 4         | 4 btn (Main Lights, Ambient, Spots, Relay Control) |
| 8    | 9         | 5 btn + 4 slider |
| 9    | 9         | 5 btn + 4 slider |
| 10   | 10        | 7 btn + 3 slider |
| 11   | 5         | 2 btn + 2 slider + 1 cpicker |
| 12   | 5         | 2 btn + 2 slider + 1 cpicker |

---

## Data Flow: Touch Handling

1. User clicks/taps on plate screenshot in browser
2. Frontend converts pixel coords to plate coords (0-480 range)
3. POST to `/api/touch/<name>` with `{x, y, state: 0}` (state 0 = release)
4. Backend calls `find_object_at(name, x, y)` to find the target object
5. Based on object type:
   - **slider**: calculate relative position → set val
   - **arc**: calculate relative position → set val
   - **toggle** (any type with `toggle: true`): flip current val (0↔1)
   - **everything else**: generic tap (down+up events)
6. Publish to BOTH topics:
   - `hasp/<name>/state/pXbY` — triggers HA automations
   - `hasp/<name>/command/pXbY.val` — triggers firmware GPIO/relay bindings
7. Force delayed screenshot refresh (0.4s) to capture the visual change

---

## Frontend Architecture

### Long-poll loop
- `startLongPoll(plate)` → calls `/api/wait/<name>?v=N`
- Blocks until screenshot version changes or 25s timeout
- On response: update screenshot image, update page indicator
- On page change: immediately update `objectMaps[plate].page` + refetch object map

### Object map
- `fetchObjectMap(plateName)` → calls `/api/objects/<name>`
- Response: `{page: N, objects: {"0": [...], "7": [...], ...}}`
- Stored in `objectMaps[plateName]`

### Hover highlight
- On mousemove: `findObjectAt(plate, x, y)` → show highlight box around matched object
- Must use topmost-wins (last match) same as backend

### Cache busting
- Static files loaded with `?v=<version>` query parameter
- Prevents browser from serving stale JS/CSS after addon update

---

## MQTT Setup

### Subscription topics (set in `_on_connect`)
- `hasp/<name>/state/#` — all state messages for configured plates
- `hasp/<name>/LWT` — online/offline status
- `hasp/+/LWT` — auto-discovery
- `hasp/+/state/statusupdate` — auto-discovery

### Publish topics (touch commands)
- `hasp/<name>/state/pXbY` — fake state events (for HA automations)
- `hasp/<name>/command/pXbY.val` — direct value commands (for firmware)
- `hasp/<name>/command` — page next/prev commands

### Connection
- Gets broker credentials from `http://supervisor/services/mqtt`
- Uses paho-mqtt 1.6.1 client
- Reconnects automatically on disconnect

---

## Files

| File | Purpose |
|------|---------|
| `openhasp_mirror/app.py` | Flask backend — all server logic |
| `openhasp_mirror/static/app.js` | Frontend JS — long-poll, touch, highlight |
| `openhasp_mirror/static/style.css` | Dark theme styles |
| `openhasp_mirror/templates/index.html` | HTML template with ingress base path |
| `openhasp_mirror/config.yaml` | HA addon metadata (version, slug, arch) |
| `openhasp_mirror/Dockerfile` | Python 3.11-alpine, flask+pillow+paho-mqtt |
| `CHANGELOG.md` | Version history |
| `repository.yaml` | HA addon repository metadata |

---

## Debug Checklist

When clicks don't work, check in order:

1. **Page tracking**: Does footer show correct page? (e.g. "Page 7 (4 obj)")
   - If wrong page → page tracking broken
   - If right page but 0 obj → object map not loaded or filter too strict

2. **Object map**: GET `/api/objects/<name>` — does it list objects for the current page?
   - If empty → `fetch_objects` failed (check plate IP, pages.jsonl accessibility)
   - If populated but wrong page → `get_current_page` returning wrong value

3. **MQTT**: GET `/api/debug/<name>` — is `mqtt_connected` true?
   - If false → MQTT credentials failed, broker unreachable
   - If true but `current_page_mqtt` is "NOT SET" → page messages not received

4. **HA entity**: Is `sensor.<plate>_page` updating in HA Developer Tools?
   - If yes → can use as fallback even without MQTT
   - If no → plate MQTT might be misconfigured

5. **Browser cache**: Check page source — does app.js URL have `?v=` parameter?
   - If no → stale JS cached, hard refresh (Ctrl+Shift+R)
