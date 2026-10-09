"""Issue a node's federation key from the hub's master secret.

    VOLTSENTRY_FED_MASTER=... python -m federation.keys VS-CHENNAI

Run it where the master secret lives (the hub), then hand the printed key to
that site only, as --fed-key / VOLTSENTRY_FED_KEY. A key works for that one
node id and no other.
"""

from __future__ import annotations

import argparse
import os
import sys

from federation.protocol import DEV_MASTER_SECRET, derive_node_key


def main() -> None:
    p = argparse.ArgumentParser(description="Derive a VoltSentry node's federation key")
    p.add_argument("node_id")
    p.add_argument("--master-secret", default=os.environ.get("VOLTSENTRY_FED_MASTER", DEV_MASTER_SECRET))
    a = p.parse_args()
    if a.master_secret == DEV_MASTER_SECRET:
        print("warning: deriving from the development master secret", file=sys.stderr)
    print(derive_node_key(a.master_secret, a.node_id))


if __name__ == "__main__":
    main()
