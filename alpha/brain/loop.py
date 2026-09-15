"""Agent loop (M6): planner/executor, tools, recipes. Provider-neutral."""

from __future__ import annotations


class AgentLoop:
    def __init__(self, planner, grounder, summarizer):
        self.planner = planner
        self.grounder = grounder
        self.summarizer = summarizer

    async def run(self, request: str) -> str:
        raise NotImplementedError("M6")