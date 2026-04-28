# Changelog

## [1.0.0] - 2026-04-28

### Features
- **Live screenshot mirroring**: BMP-to-JPEG proxy with auto-detected plate resolution
- **Remote touch control**: hit-tests click coordinates against interactive objects from `/pages.jsonl`, publishes MQTT events to trigger HA automations
- **MQTT-driven architecture**: subscribes to plate MQTT topics, only fetches screenshot on meaningful events — zero polling
- **Long-polling frontend**: browser blocks until a new screenshot is ready — instant updates, zero wasted requests
- **Auto-discovery**: new plates appear automatically via MQTT `statusupdate` — no config needed
- **Mobile touch support**: touchstart/touchmove/touchend events for phone/tablet use
- **Object highlight on hover**: interactive objects glow when mouse hovers, showing object ID and type
- **Swipe gesture**: horizontal swipe changes plate page (next/prev)
- **Plate offline detection**: LWT tracking with pulsing red indicator and "OFFLINE" status
- **Debug endpoint**: `GET /api/debug/<name>` shows MQTT state, page tracking, refresh diagnostics
- **Dark theme UI** with HA Ingress sidebar integration

### Performance
- **Viewer-aware refresh**: screenshot fetches only happen when someone has the mirror open in a browser. No viewer = no fetches = zero plate interference
- **Screenshot serialization**: only one HTTP fetch runs per plate at a time, preventing ESP32 overload
- **Screenshot hash dedup**: MD5 comparison skips identical frames
- **1.5s debounce**: gives ESP32 time to finish rendering before screenshot fetch
- **30s periodic fallback**: catches HA-driven visual updates (temp, time, weather) without per-command fetches
- **HA commands ignored for refresh**: continuous streams (camera feed, temp updates) don't trigger fetches
- **Cached config**: `/data/options.json` cached for 5s
- **Async startup**: plate init runs in background thread — Flask serves immediately

### Security
- **SSRF protection**: plate IPs validated as private, non-loopback
- **XSS fix**: `base_path` uses Jinja2 `|tojson` filter
- **MQTT injection fix**: page direction validated to `next`/`prev` only
- **Info leak fix**: error responses don't expose internal IPs/hostnames
- **Debug endpoint sanitized**: no MQTT host/user, no entity IDs, no object values
- **Security headers**: `X-Content-Type-Options: nosniff`, `X-Frame-Options: SAMEORIGIN`
- **Touch coordinate validation**: x/y clamped to plate resolution bounds
- **Page number validation**: 0-255 range enforced
- **Plate name validation**: must match `[a-zA-Z0-9_-]+`
- **Pinned dependencies**: all pip packages pinned in Dockerfile
