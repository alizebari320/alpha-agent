"""Provider-neutral tool schemas (§3 critical architectural consequence).

AgentRouter/GLM are OpenAI-compatible and do NOT support Anthropic's native
`computer` tool schema. Alpha defines its OWN JSON function tools; every
provider maps onto these. The agent loop only speaks THESE.

  screenshot()                    -> image + element list (handled by the loop)
  move(x,y) / click(x,y,button)   -> absolute screen coords (model space)
  click_element(label)            -> SoM label; PREFERRED over raw coords
  double_click / right_click / drag / scroll
  type_text(text)
  key(combo)                      -> "ctrl+l", "Return", "alt+Tab"
  bash(command)                   -> launch apps (allowlisted)
  wait(ms)
  finish(answer)                  -> 1-2 spoken sentences, ALWAYS required
"""

from __future__ import annotations


def tool_schemas() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "click_element",
                "description": ("Click a UI element identified by its numbered "
                                "Set-of-Mark label from the element table. "
                                "PREFERRED over raw coordinates."),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "the SoM label, e.g. '14'"},
                    },
                    "required": ["label"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": "Click at raw coordinates in the CURRENT screenshot's coordinate space.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"],
                                   "description": "default left"},
                    },
                    "required": ["x", "y"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "move",
                "description": "Move the cursor to coordinates (no click).",
                "parameters": {"type": "object",
                               "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                               "required": ["x", "y"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "double_click",
                "description": "Double-click at coordinates.",
                "parameters": {"type": "object",
                               "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                               "required": ["x", "y"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "right_click",
                "description": "Right-click at coordinates.",
                "parameters": {"type": "object",
                               "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
                               "required": ["x", "y"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "drag",
                "description": "Drag from (x1,y1) to (x2,y2).",
                "parameters": {"type": "object",
                               "properties": {"x1": {"type": "number"}, "y1": {"type": "number"},
                                              "x2": {"type": "number"}, "y2": {"type": "number"}},
                               "required": ["x1", "y1", "x2", "y2"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "scroll",
                "description": "Scroll. dx horizontal, dy vertical (positive = down/left).",
                "parameters": {"type": "object",
                               "properties": {"dx": {"type": "number"}, "dy": {"type": "number"}},
                               "required": []},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "type_text",
                "description": "Type text at the focused element.",
                "parameters": {"type": "object",
                               "properties": {"text": {"type": "string"}},
                               "required": ["text"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "key",
                "description": ("Press a key combo, e.g. 'ctrl+l' (Firefox URL bar), "
                                "'Return', 'alt+Tab', 'ctrl+t'. PREFER KEYBOARD "
                                "SHORTCUTS over clicking."),
                "parameters": {"type": "object",
                               "properties": {"combo": {"type": "string"}},
                               "required": ["combo"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": ("Run a shell command (for launching apps). "
                                "Prefer this over clicking through menus: "
                                "e.g. bash('firefox') or bash('xdg-open https://x')"),
                "parameters": {"type": "object",
                               "properties": {"command": {"type": "string"}},
                               "required": ["command"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "wait",
                "description": "Wait milliseconds for the UI to respond.",
                "parameters": {"type": "object",
                               "properties": {"ms": {"type": "number"}},
                               "required": ["ms"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "finish",
                "description": ("Call when the request is complete (or impossible). "
                                "answer: 1-2 short sentences, spoken aloud. "
                                "ALWAYS call this at the end."),
                "parameters": {"type": "object",
                               "properties": {"answer": {"type": "string"}},
                               "required": ["answer"]},
            },
        },
    ]


VALID_TOOLS = {t["function"]["name"] for t in tool_schemas()}
