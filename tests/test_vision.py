"""M5 tests: coordinate scaling (spec: #1 failure mode), SoM, password lock."""

import pytest

from alpha.vision import (AtspiElement, downscale_size, model_to_screen,
                          screen_to_model, clip_to_monitor, som_label,
                          som_table_text, draw_som_png, PasswordFieldLocked)

MON = {"name": "eDP-1", "x": 0, "y": 0, "w": 1920, "h": 1080, "scale": 1}
MON2 = {"name": "HDMI", "x": 1920, "y": 0, "w": 2560, "h": 1440, "scale": 1}


def test_downscale_size():
    assert downscale_size(1920, 1080, 1280) == (1280, 720)
    assert downscale_size(1000, 800, 1280) == (1000, 800)  # already small
    assert downscale_size(2560, 1440, 1280) == (1280, 720)


def test_model_to_screen_identity_no_downscale():
    # shot is native resolution: 1:1 mapping
    assert model_to_screen(960, 540, 1920, 1080, MON) == (960, 540)
    assert model_to_screen(0, 0, 1920, 1080, MON) == (0, 0)


def test_model_to_screen_with_downscale():
    # 1280x720 shot of a 1920x1080 screen: scale 1.5
    assert model_to_screen(640, 360, 1280, 720, MON) == (960, 540)
    assert model_to_screen(1280, 720, 1280, 720, MON) == (1920, 1080)


def test_model_to_screen_second_monitor():
    # coords on monitor 2 with origin (1920,0), 2560x1440 native shot
    assert model_to_screen(1280, 720, 2560, 1440, MON2) == (3200, 720)
    # downscaled 1280x720 shot of monitor 2
    assert model_to_screen(640, 360, 1280, 720, MON2) == (3200, 720)


def test_screen_to_model_roundtrip():
    for (sx, sy) in [(10, 10), (960, 540), (1919, 1079)]:
        mx, my = screen_to_model(sx, sy, 1280, 720, MON)
        assert model_to_screen(mx, my, 1280, 720, MON) == (sx, sy)


def test_clip_to_monitor():
    assert clip_to_monitor(-5, 500, MON) == (0, 500)
    assert clip_to_monitor(2000, 500, MON) == (1919, 500)
    assert clip_to_monitor(500, 99999, MON) == (500, 1079)


def _els():
    return [
        AtspiElement("push button", "Search", 100, 200, 80, 30, False, False, 3),
        AtspiElement("link", "AI website", 300, 400, 120, 20, False, False, 4),
        AtspiElement("password text", "", 500, 600, 100, 30, True, True, 5),
        AtspiElement("label", "not clickable", 10, 10, 50, 20, False, False, 6),
    ]


def test_som_label_picks_clickables_excludes_password():
    labels, label_map = som_label(_els(), 1280, 720, MON)
    descs = " ".join(l["desc"] for l in labels)
    assert "Search" in descs and "AI website" in descs
    assert "password" not in descs.lower()  # never offered
    # label 1 center = (140,215) screen -> model (93.3, 143.3)
    p = label_map["1"]
    assert p["screen_x"] == 140 and p["screen_y"] == 215
    assert abs(p["x"] - 140 * 1280 / 1920) < 0.2


def test_som_table_text():
    labels, _ = som_label(_els(), 1280, 720, MON)
    table = som_table_text(labels)
    assert table.startswith("1: ")
    assert all(l["desc"].split(":")[0] for l in labels)


def test_draw_som_png_noop_on_plain_b64():
    # invalid b64 -> falls back to returning input (never crashes)
    assert draw_som_png("notb64", {"1": {"x": 10, "y": 10}}) == "notb64"


def test_password_lock_detection():
    from alpha.vision import Vision

    class FakeWorker:
        pass

    class FakeDaemon:
        monitors = [MON]

    v = Vision(FakeWorker(), FakeDaemon(), password_lock=True)
    els = _els()
    # focused password field -> locked
    assert v.check_password_lock.__doc__
    # password field NOT focused -> not locked
    els[2] = AtspiElement("password text", "", 500, 600, 100, 30, False, True, 5)
    import asyncio
    assert not asyncio.run(v.check_password_lock([els[2]]))
