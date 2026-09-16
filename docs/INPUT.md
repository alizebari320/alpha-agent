# Input injection — how Alpha moves the mouse and types

This is the part of the project that took the longest to get right, so the
reasoning and the evidence are recorded here in full. Read this before changing
`alpha/input/`.

## Summary

Alpha injects input through **`/dev/uinput`** using a **separate absolute-axis
pointer device and a separate keyboard device**. It needs no root, no
`ydotool` daemon, no `sudo`, and no compositor-specific protocol — the same code
works on GNOME Wayland and on X11.

```toml
[input]
backend = "auto"     # auto | uinput | x11  (auto picks uinput when /dev/uinput is writable)
```

Verify it yourself:

```bash
scripts/test-input.py        # opens a GTK4 probe window and checks 7 behaviours
```

## Why not the obvious approaches

| Approach | Verdict on GNOME Wayland |
|---|---|
| `xdotool` (XTEST) | `mousemove` reports success but the real cursor never moves. XTEST events from Xwayland are dropped by Mutter in this session. Kept only as the X11 backend. |
| `ydotool` | Correct tool for wlroots, but it needs a **root-owned** `ydotoold` daemon plus udev rules — Alpha is a `systemd --user` service with no root, and the machine had no passwordless sudo. |
| `org.freedesktop.portal.RemoteDesktop` | The "proper" Wayland way, and it works, but it requires an interactive approval dialog on every session and gives *relative* pointer motion with its own acceleration. Unusable for an always-on assistant. |
| **`/dev/uinput`** | ✅ Kernel-level virtual device. Mutter treats it exactly like the user's real Logitech mouse/keyboard. |

## The three findings that mattered

### 1. Mutter adds hotplugged devices asynchronously — a settle delay is mandatory

Creating a uinput device and immediately writing events silently loses them.
Mutter opens hotplugged input devices from its **main loop**, so events sent
before it has the device open go nowhere. The daemon logs showed gnome-shell
holding `event25`/`event26` *after* they appeared, which is the proof.

Fix in `uinput.py`: after `UI_DEV_CREATE`, poll for the `/dev/input/eventN` node
that belongs to the device and wait `READY_SETTLE_S = 2.5` further seconds.
With the delay, a Super-key toggle moves GNOME's overview `false → true → false`
reliably; without it, nothing happens (verified with
`gdbus … org.gnome.Shell OverviewActive`).

The daemon also **pre-warms** the backend at startup (`input backend ready:
UInputBackend`) so the first request never pays this cost.

### 2. A relative pointer is not pixel-exact — pointer acceleration destroys it

The first implementation used `EV_REL` deltas with corner-anchoring (drive the
pointer into the top-left corner, then move by relative offsets). It looked
correct in the audit log and missed its target on screen: **libinput applies
pointer acceleration to relative motion**, nonlinearly, so "move 871 px right"
does not land 871 px right.

Fix: the pointer device declares **absolute axes**:

```python
absbits=[evdev.ABS_X, evdev.ABS_Y],
input_props=[evdev.INPUT_PROP_POINTER],
```

libinput then classifies it as an absolute pointer, exactly like a graphics
tablet in absolute mode. Result, measured with the GTK4 probe: requested
(1500, 900) → reported (1500, 900). Pixel-exact, no acceleration, no drift.

### 3. One "kitchen sink" device gets misclassified as a joystick

The first version put keys, buttons, relative **and** absolute axes on one
device. udev classified it as a joystick (`js0`) and Mutter ignored it as a
pointer. Two clean devices fixed it — and the udev properties now match the
user's real hardware:

```
alpha-agent pointer   event25   ID_INPUT=1 ID_INPUT_MOUSE=1
alpha-agent keyboard  event26   ID_INPUT=1 ID_INPUT_KEY=1 ID_INPUT_KEYBOARD=1
```

`tests/test_misc.py::test_uinput_device_requirements` guards against
regressing to the joystick layout.

## Device layout

| Device | Capabilities | Used for |
|---|---|---|
| `alpha-agent pointer` | `EV_ABS` ABS_X/ABS_Y (0…max from HUD geometry), `EV_KEY` BTN_LEFT/RIGHT/MIDDLE, `EV_REL` REL_WHEEL/HWHEEL, `INPUT_PROP_POINTER` | `move`, `click`, `double_click`, `right_click`, `drag`, `scroll` |
| `alpha-agent keyboard` | `EV_KEY` for the full key table in `evdev.py` | `type_text`, `key` (combos like `ctrl+l`) |

Both are created on first use and stay open for the daemon's lifetime.

## Coordinate space

The model sees a **downscaled** screenshot (`vision.max_width = 1280`), so the
planner's coordinates are in image space. Conversion (unit-tested — the spec
calls coordinate bugs the number-one failure mode):

```
model_to_screen = model * (real_w / shot_w) + monitor_origin
screen_to_model = (screen - monitor_origin) * (shot_w / real_w)
```

`move_abs` interpolates in several steps so the compositor sees a plausible
motion stream rather than a teleport (some apps ignore single-jump pointers).

## Known limitation on GNOME Wayland

Mutter does not expose `zwlr_layer_shell_v1`, and it treats some synthetic
input paths conservatively — notably, grabbing global shortcuts from a virtual
device is not something we rely on. That is why:

- The abort hotkey is a **GNOME custom keybinding** (`alpha install-hotkeys`),
  not a device grab.
- The HUD cannot always be pinned above fullscreen windows on Wayland; GTK4
  layer-shell is used when available and a warning is logged otherwise.

For guaranteed always-on-top behaviour, log into **GNOME on Xorg** — the input
backend, HUD and vision code paths are identical there and all are exercised by
the same tests.

## Permissions

`/dev/uinput` is `root:input 0660`; the installing user must be in the `input`
group (the installer checks and offers the `usermod` command — it never runs
`sudo` behind your back). After adding the group you must log out and back in.

If `/dev/uinput` is not writable, `detect_backend()` falls back to X11
(`xdotool`) when `$DISPLAY` is set, and otherwise raises a clear error instead
of pretending to work.
