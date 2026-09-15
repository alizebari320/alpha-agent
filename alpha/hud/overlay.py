"""Floating HUD overlay (M3). GTK4 + gtk4-layer-shell always-on-top box."""

from __future__ import annotations


class Overlay:
    def show(self, text: str, state: str = "idle") -> None:
        raise NotImplementedError("M3")

    def hide(self) -> None:
        raise NotImplementedError("M3")