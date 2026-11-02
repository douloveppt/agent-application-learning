#!/usr/bin/env python3
"""Observe local simulated I/O; no network, database, LLM or background worker."""
from __future__ import annotations

import argparse
import asyncio
import json
import time

MODES = ("sequential", "concurrent", "blocking", "timeout", "failure")


async def run_mode(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    started = time.perf_counter()
    events = []
    results = []
    status = "ok"

    def record(task, event):
        events.append({"task": task, "event": event,
                       "at_ms": round((time.perf_counter() - started) * 1000, 1)})

    async def lookup(name, delay, *, blocking=False, fail=False):
        record(name, "started")
        try:
            if blocking:
                time.sleep(delay)  # Deliberate mistake: blocks the event loop.
            else:
                await asyncio.sleep(delay)  # Simulated I/O yields control.
            if fail:
                record(name, "failed")
                raise RuntimeError("Simulated knowledge lookup failure")
            record(name, "completed")
            return {"source": name, "value": "simulated result"}
        except asyncio.CancelledError:
            record(name, "cancelled")
            raise

    if mode == "sequential":
        results.append(await lookup("knowledge", 0.15))
        results.append(await lookup("profile", 0.10))
    elif mode in ("concurrent", "blocking"):
        results = await asyncio.gather(
            lookup("knowledge", 0.15, blocking=mode == "blocking"),
            lookup("profile", 0.10, blocking=mode == "blocking"),
        )
    elif mode == "timeout":
        try:
            results.append(await asyncio.wait_for(lookup("knowledge", 0.25), timeout=0.03))
        except TimeoutError:
            status = "timeout"
            record("request", "timeout_observed")
    elif mode == "failure":
        try:
            async with asyncio.TaskGroup() as group:
                knowledge = group.create_task(lookup("knowledge", 0.015, fail=True))
                profile = group.create_task(lookup("profile", 0.25))
            results = [knowledge.result(), profile.result()]
        except* RuntimeError:
            status = "failed"
            record("request", "failure_observed")
    return {"mode": mode, "status": status,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "results": results, "events": events}


async def run_selected(mode):
    return [await run_mode(item) for item in MODES] if mode == "all" else await run_mode(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=(*MODES, "all"), default="all")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_selected(args.mode)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
