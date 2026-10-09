"""Start a two-site VoltSentry network on one machine.

    python scripts/run_network.py                    # concise console, full logs in logs/
    python scripts/run_network.py --verbose          # stream every process
    python scripts/run_network.py --monitor chennai  # Chennai as a new site: detect, never block

What starts (the same processes you would run on separate machines):

    central CSMS        ws://localhost:9000               one per operator, shared by every site
    federation hub      ws://localhost:7700/node          VoltSentry nodes dial in
                        ws://localhost:7701/network       network view for the dashboard
    site A  Vellore     proxy :8000 / feed :8100          8 bays  CP-01..CP-08   control :9100
    site B  Chennai     proxy :8001 / feed :8101          6 bays  CHN-01..CHN-06  control :9101

Ctrl-C stops everything.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
PY = sys.executable

# Lines worth showing on a concise console; everything else goes to the log file.
HIGHLIGHT = ("THREAT", "QUARANTINE", "federation", "joined", "disconnected", "IoC", "cloned",
             "refused", "WARNING", "Error", "error", "Traceback", "Listening", "listening", "ingress", "mode:")

SITES = [
    {"key": "vellore", "node": "VS-VELLORE", "name": "Vellore Campus Hub", "region": "Tamil Nadu",
     "lat": "12.9692", "lon": "79.1559", "ingress": 8000, "feed": 8100, "control": 9100,
     "prefix": "CP", "bays": 8, "seed": 7},
    {"key": "chennai", "node": "VS-CHENNAI", "name": "Chennai Highway Hub", "region": "Tamil Nadu",
     "lat": "13.0827", "lon": "80.2707", "ingress": 8001, "feed": 8101, "control": 9101,
     "prefix": "CHN", "bays": 6, "seed": 11},
]


def plan(monitor: set[str] = frozenset()) -> list[tuple[str, list[str], float]]:
    """(label, argv, delay-before-start)."""
    steps: list[tuple[str, list[str], float]] = [
        ("csms", [PY, "-m", "simulator.csms", "--port", "9000"], 0.0),
        ("hub", [PY, "-m", "federation.hub", "--port", "7700", "--feed-port", "7701"], 0.0),
    ]
    for i, s in enumerate(SITES):
        steps.append((f"{s['key']}-proxy", [
            PY, "-m", "proxy.main", "--node-id", s["node"], "--site-name", s["name"], "--region", s["region"],
            "--lat", s["lat"], "--lon", s["lon"], "--bays", str(s["bays"]),
            "--ingress-port", str(s["ingress"]), "--feed-port", str(s["feed"]),
            "--csms-url", "ws://localhost:9000", "--hub-url", "ws://localhost:7700/node",
            "--mode", "monitor" if s["key"] in monitor else "enforce",
        ], 1.5 if i == 0 else 0.2))
    for i, s in enumerate(SITES):
        steps.append((f"{s['key']}-twin", [
            PY, "-m", "simulator.twin", "--target", f"ws://localhost:{s['ingress']}",
            "--stations", str(s["bays"]), "--prefix", s["prefix"],
            "--control-port", str(s["control"]), "--seed", str(s["seed"]),
        ], 1.5 if i == 0 else 0.2))
    return steps


def pump(label: str, proc: subprocess.Popen, log_path: Path, verbose: bool) -> None:
    with log_path.open("w", encoding="utf-8") as fh:
        for line in proc.stdout:  # type: ignore[union-attr]
            fh.write(line)
            fh.flush()
            if verbose or any(h in line for h in HIGHLIGHT):
                sys.stdout.write(f"{label:>15} | {line}")
                sys.stdout.flush()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true", help="stream every line from every process")
    ap.add_argument("--monitor", default="", metavar="SITES",
                    help="comma-separated sites to run in monitor-only mode, e.g. chennai")
    args = ap.parse_args()
    monitor = {m.strip().lower() for m in args.monitor.split(",") if m.strip()}
    unknown = monitor - {s["key"] for s in SITES}
    if unknown:
        ap.error(f"unknown site(s): {', '.join(sorted(unknown))}; choose from {', '.join(s['key'] for s in SITES)}")

    LOGS.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(ROOT)}
    procs: list[tuple[str, subprocess.Popen]] = []

    def stop(*_):
        for _, p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for _, p in procs:
            try:
                p.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
        print("\nnetwork stopped.")
        sys.exit(0)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    for label, argv, delay in plan(monitor):
        time.sleep(delay)
        proc = subprocess.Popen(argv, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        procs.append((label, proc))
        threading.Thread(target=pump, args=(label, proc, LOGS / f"{label}.log", args.verbose), daemon=True).start()

    print("\nVoltSentry network up. Full logs: logs/<process>.log")
    print("  dashboard:        cd dashboard && npm run dev   ->  http://localhost:3000  (Network tab)")
    print("  attack Vellore:   python -m cli.attack -a 1                      (meter spoof on CP-03)")
    print("  clone a card:     python -m cli.attack -a 5 -s CHN-02 -t TAG-CP-03 --control-url ws://localhost:9101/control")
    print("  reset a site:     python -m cli.attack -a r [--control-url ws://localhost:9101/control]")
    print("  health:           curl localhost:8100/healthz   curl localhost:7701/healthz\n")

    # A process dying is reported, not fatal: the sites are independent, which
    # is the point (stop the hub and both sites keep protecting themselves).
    reported: set[str] = set()
    while True:
        time.sleep(1)
        for label, p in procs:
            if p.poll() is not None and label not in reported:
                reported.add(label)
                print(f"{label} exited with code {p.returncode}; see logs/{label}.log. Other processes keep running.")


if __name__ == "__main__":
    main()
