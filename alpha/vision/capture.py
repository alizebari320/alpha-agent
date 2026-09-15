"""Vision + grounding (M5): capture, AT-SPI, SoM. See docs for coordinate-scaling."""

from __future__ import annotations


def capture(monitor: str | None = None, max_width: int = 1280):
    raise NotImplementedError("M5")


def atspi_tree():
    raise NotImplementedError("M5")


def draw_som(image, elements):
    raise NotImplementedError("M5")