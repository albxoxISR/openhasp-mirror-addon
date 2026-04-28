# openHASP Mirror Add-on for Home Assistant

Live screen mirror and remote touch control for any [openHASP](https://openhasp.com) plate, directly from your Home Assistant sidebar.

## Features

- **MQTT-driven updates** — subscribes to plate MQTT topics, only fetches a screenshot when something actually changes on the plate (zero load on idle plates)
- **Long-polling frontend** — the browser instantly receives updates when a new screenshot is available, no wasted polling requests
- **Remote touch control** — click or tap anywhere on the mirrored screen to interact with the plate as if touching it physically
- **Mobile support** — full touch events (touchstart/touchmove/touchend) for phone and tablet use
- **Object highlight** — interactive objects glow when you hover over them, showing the object ID and type
- **Swipe gestures** — swipe left/right on the plate screen to change pages
- **Auto-discovery** — plates are automatically discovered from MQTT, no manual configuration needed
- **Multi-plate dashboard** — display and control multiple plates side by side in a responsive card grid
- **Screenshot hash dedup** — MD5 comparison skips identical frames even if MQTT fires multiple events
- **Generic** — works with any openHASP plate on your network, no firmware changes required
- **HA Ingress** — accessible from the Home Assistant sidebar, no extra ports to expose

## How it works

1. **MQTT subscription** — the addon connects to your MQTT broker and subscribes to `hasp/<plate>/state/#` for all configured and auto-discovered plates
2. **Smart screenshots** — when MQTT activity is detected (button press, page change, value update), the addon fetches a screenshot, converts BMP to JPEG, and compares the hash to skip duplicates
3. **Long-polling** — the frontend holds an open request to `/api/wait/<name>` that returns instantly when a new screenshot is ready
4. **Touch simulation** — on click/tap, the addon finds the interactive object at the coordinates (from `/pages.jsonl`), then publishes to both the state topic (triggers HA automations) and the command topic (triggers firmware GPIO/relay bindings)
5. **Auto-discovery** — listens for `hasp/+/state/statusupdate` messages which contain the plate IP, automatically adding new plates to the dashboard

## Installation

### Add the repository

1. In Home Assistant go to **Settings > Add-ons > Add-on Store**
2. Click the three-dot menu (top right) > **Repositories**
3. Paste: `https://github.com/albxoxISR/openhasp-mirror-addon`
4. Click **Add**, then refresh the page

### Install the add-on

1. Find **openHASP Mirror** in the add-on store and click **Install**
2. Go to the **Configuration** tab and add your plates (optional — plates are also auto-discovered from MQTT):

```yaml
plates:
  - name: "plate01"
    ip: "192.168.1.100"
  - name: "plate02"
    ip: "192.168.1.101"
```

> **name** must match the plate's MQTT node name in openHASP configuration (used for MQTT topics).
> **ip** is the plate's local IP address.

3. Start the add-on and enable **Show in sidebar**

## Configuration

| Option | Type | Default | Description |
|---|---|---|---|
| `plates` | list | `[]` | List of plates, each with `name` (string) and `ip` (string). Leave empty to rely on auto-discovery. |

## Requirements

- openHASP plates running firmware 0.7.x or later
- MQTT broker configured in Home Assistant
- Plates must be reachable via HTTP from the HA host

## API Endpoints

All endpoints are served behind HA Ingress.

| Method | Path | Description |
|---|---|---|
| GET | `/api/plates` | List configured + auto-discovered plates with resolution |
| GET | `/api/wait/<name>?v=N` | Long-poll — blocks until screenshot version > N |
| GET | `/api/screenshot/<name>` | Cached JPEG screenshot (ETag support) |
| POST | `/api/touch/<name>` | Send touch event `{x, y, state}` |
| POST | `/api/page/<name>` | Change page `{dir: "next"\|"prev"}` |
| GET | `/api/objects/<name>` | View cached object map (debug) |
| POST | `/api/objects/<name>/refresh` | Re-fetch pages.jsonl from plate |
| GET | `/api/debug/<name>` | Internal state dump (MQTT, page, objects) |

## Troubleshooting

- **Plates show "offline"** — verify the plate IP is correct and reachable from HA (`ping <ip>`)
- **No updates appearing** — check that MQTT is working: the addon needs `hasp/<plate>/state/#` messages to know when to refresh
- **Clicks don't work** — ensure the plate's MQTT node name matches the `name` in addon config, and that your HA automations use MQTT triggers on `hasp/<name>/state/...` topics
- **Relay buttons don't respond** — the addon publishes to both the state topic (HA automations) and command topic (firmware GPIO); verify the plate's relay GPIO config
- **Wrong objects respond** — POST to `/api/objects/<name>/refresh` to re-fetch the plate's layout after uploading new pages
- **Auto-discovered plates missing** — plates broadcast `statusupdate` on boot; restart the plate or manually add it to the configuration

## License

MIT
