"""Record the live dashboard feed to a JSONL dataset for Tier-2 training.

Connects to the proxy fanout (ws://localhost:8100/feed) and writes every event,
one JSON object per line, until --seconds elapse. Run it against a CLEAN fleet
(no attacks) to capture honest CC-CV sessions; proxy.train_tier2 then fits the
baseline on these and synthesises the attacks itself.

    # terminal 1-3: python -m simulator.csms / proxy.main / simulator.twin
    python -m proxy.capture_dataset --out proxy/data/honest_replay.jsonl --seconds 240
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from pathlib import Path

import websockets

DEFAULT_URL = "ws://localhost:8100/feed"


async def capture(url: str, out: Path, seconds: float) -> Counter:
    out.parent.mkdir(parents=True, exist_ok=True)
    counts: Counter = Counter()
    deadline = asyncio.get_event_loop().time() + seconds
    with out.open("w", encoding="utf-8") as fh:
        async with websockets.connect(url, max_size=None) as ws:
            print(f"[capture] recording {url} -> {out} for {seconds:.0f}s", flush=True)
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except asyncio.TimeoutError:
                    break
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                counts[ev.get("event", "?")] += 1
                line = raw if isinstance(raw, str) else raw.decode()
                fh.write(line.rstrip("\n") + "\n")
    return counts


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture the VoltSentry feed to JSONL")
    ap.add_argument("--out", default="proxy/data/honest_replay.jsonl")
    ap.add_argument("--seconds", type=float, default=240.0)
    ap.add_argument("--url", default=DEFAULT_URL)
    args = ap.parse_args()

    out = Path(args.out)
    counts = asyncio.run(capture(args.url, out, args.seconds))
    total = sum(counts.values())
    print(f"[capture] wrote {total} events to {out}", flush=True)
    for name, n in sorted(counts.items()):
        print(f"[capture]   {name}: {n}", flush=True)


if __name__ == "__main__":
    main()
