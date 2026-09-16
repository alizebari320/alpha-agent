# Vision — screenshots, accessibility tree, Set-of-Mark

Alpha's "eyes" have three layers, and it degrades gracefully through them:

1. **Screenshot** (portal ScreenCast → PipeWire → PNG) — needs one-time consent
   and a vision-capable LLM.
2. **Accessibility tree** (AT-SPI) of the focused window — always available,
   text-only, no consent needed.
3. **Set-of-Mark tables** — numbered labels drawn over the screenshot and
   listed as text, so a text-only model can still say "click 7".

## Where the code lives

The daemon (venv Python) has no PyGObject, so all GTK/GStreamer/Atspi work
happens in the **HUD child process**, launched with system `/usr/bin/python3`:

| File | Role |
|---|---|
| `alpha/hud/app.py` | GTK4 overlay **and** the desktop worker: serves `screenshot`, `screen-init`, `atspi` requests from `hud.sock` and replies on `ctl.sock` |
| `alpha/hud/worker.py` | `ScreenCast` (portal + GStreamer) and `atspi_focused_tree()` |
| `alpha/vision/__init__.py` | daemon-side facade: request/response with correlation ids, coordinate math, SoM helpers |

Sockets (both mode `0600`, under `~/.local/state/alpha/`):

- `hud.sock` — daemon → HUD: `{"type":"state"|"mute"|"req","id":N,"op":…}`
- `ctl.sock` — HUD → daemon replies (`{"cmd":"res","id":N,…}`) plus CLI commands

## Screenshots: the portal dance

`op = "screen-init"` runs `org.freedesktop.portal.ScreenCast`:

1. `CreateSession` → `SelectSources` (monitor, cursor, restore token) →
   `Start` → the compositor shows **one** permission dialog.
2. The resulting PipeWire fd is handed to a GStreamer pipeline
   (`pipewiresrc ! videoconvert ! appsink`), and frames are pulled on demand.
3. The restore token is persisted, so with "Always allow" you are only asked
   once per token lifetime.

**Until you approve that dialog, screenshots time out.** That is expected, not a
bug: `Vision.screenshot()` catches the timeout, returns `None`, logs
`grant screen sharing in the Alpha HUD (Share button)` exactly once, and the
agent continues with text-only grounding.

Verify:

```bash
alpha state          # confirms the daemon and HUD are alive
journalctl --user -u alpha -f | grep -i screenshot   # look for the one warning
```

## Is my LLM able to see?

Not every model accepts images. Alpha probes once per process with a 1-pixel
image and logs the verdict:

```
INFO alpha.brain.loop: vision auto-probe: False
WARNING … vision probe failed (provider HTTP 400: … does not accept image input…)
```

When the probe fails, Alpha **never sends images** and grounds on the element
table. That is deliberate: sending a screenshot to a model that rejects images
would waste tokens and fail mid-task. Some providers even tell you which model
to use (this one answered "use `deepseek-v4.1-flash`").

## Coordinate math (the number-one failure mode)

Everything is a pure function in `alpha/vision/__init__.py` and unit-tested:

```python
downscale_size(w, h, max_width)     # 1920x1080 -> 1280x720 at max_width=1280
model_to_screen(x, y, shot, mon)    # model pixels -> global screen pixels
screen_to_model(x, y, shot, mon)    # the inverse
clip_to_monitor(x, y, mon)          # keep clicks inside the active monitor
```

Rules the code enforces:

- Model coordinates are in **screenshot image space**; the input backend wants
  **global screen space**. The origin of the active monitor is added back.
- Scale is `real_w / shot_w`, not a fixed constant — the downscaler rounds, so
  the code recomputes from the *actual* returned image size.
- Multi-monitor: the active monitor is chosen from the HUD's reported geometry
  (`alpha set-geom`); clicks are clipped so they can never land on another
  screen by accident.

## Set-of-Mark

`som_label(elements, w, h, monitor)` filters the accessibility tree down to
actionable elements (buttons, entries, links, menu items, checkboxes…),
deduplicates, and assigns stable numbers. Two renderings are produced:

- `som_table_text()` — the text table the LLM sees:
  `7  push button  "Search"  @ (812,96) 32x28  [focused]`
- `draw_som_png()` — the same numbers drawn as boxes on the screenshot, for
  vision-capable models.

The planner is instructed to prefer `click_element(label="7")` over raw pixel
coordinates whenever an element table is available.

## Reading the accessibility tree

`atspi_focused_tree()` walks the focused window (max depth 14, max 220
elements), and returns per element:

`{role, name, x, y, w, h, focused, is_password}`

Two safety-relevant details:

- `is_password` comes from the AT-SPI `PASSWORD_TEXT` state. If a focused
  element carries it, Alpha refuses to type or click (the password lock). The
  warning logs the role, name and app so a false positive can be diagnosed.
- Elements from a *background* app can appear if it holds the AT-SPI focus; the
  table is scoped to the focused window first.

## Failure modes you may actually see

| Symptom | Cause | What Alpha does |
|---|---|---|
| `atspi timed out` | AT-SPI bus busy / app not exporting a tree (some Electron apps without a11y) | returns `[]`, continues with no element table |
| screenshot timeout | portal dialog not approved, or the compositor stopped the stream | one warning, then text-only |
| empty element table + no screenshot | both layers unavailable | planner is told explicitly that it is blind and to ask you to share the screen or to use keyboard shortcuts only |
| black / stale frames | PipeWire stream ended after a monitor change | next request re-inits the stream |
