"""Federation over real sockets: a hub and two nodes on ephemeral localhost ports."""

import asyncio

import pytest
import websockets

from federation.client import FederationClient
from federation.hub import Hub
from federation.protocol import derive_node_key

MASTER = "live-test-master"


async def _until(predicate, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


def _client(url, node_id, key, inbox):
    async def on_message(kind, body):
        inbox.append((kind, body))

    return FederationClient(
        url, node_id, key, {"site_name": node_id, "bays": 2},
        heartbeat=lambda: {"charging": 1}, live_sessions=lambda: [],
        on_message=on_message, heartbeat_sec=0.2, log=lambda _m: None,
    )


@pytest.mark.asyncio
async def test_two_sites_share_sessions_and_intel_through_the_hub():
    hub = Hub(MASTER)
    server = await websockets.serve(hub.handle_node, "localhost", 0)
    url = f"ws://localhost:{server.sockets[0].getsockname()[1]}/node"
    inbox_a, inbox_b = [], []
    a = _client(url, "VS-A", derive_node_key(MASTER, "VS-A"), inbox_a)
    b = _client(url, "VS-B", derive_node_key(MASTER, "VS-B"), inbox_b)
    tasks = [asyncio.create_task(a.run()), asyncio.create_task(b.run())]
    try:
        assert await _until(lambda: a.connected and b.connected)
        assert a.tag_salt == b.tag_salt  # both can hash cards the same way

        # A card charging at site A is visible to site B, as a hash only.
        a.claim_session("TAG-1", "CP-01", live=True)
        assert await _until(lambda: b.check_card("TAG-1")[0] == ("VS-A", "CP-01"))
        assert a.check_card("TAG-1")[0] is None  # a site is not its own clone

        # Site B reports the card; site A now refuses it, and hears about it once.
        b.publish_card_ioc("TAG-1", "cloned")
        assert await _until(lambda: a.check_card("TAG-1")[1] is not None)
        b.publish_card_ioc("TAG-1", "cloned again")
        assert await _until(lambda: any(i.hits == 2 for i in hub.intel.active_iocs()))
        await asyncio.sleep(0.3)
        assert [k for k, _ in inbox_a].count("ioc") == 1

        # Session end propagates.
        a.claim_session("TAG-1", "CP-01", live=False)
        assert await _until(lambda: b.check_card("TAG-1")[0] is None)

        # Heartbeats keep both nodes online in the hub's view.
        assert await _until(lambda: hub.snapshot()["hub"]["nodes_online"] == 2)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_node_with_the_wrong_key_never_joins():
    hub = Hub(MASTER)
    server = await websockets.serve(hub.handle_node, "localhost", 0)
    url = f"ws://localhost:{server.sockets[0].getsockname()[1]}/node"
    rogue = _client(url, "VS-EVIL", derive_node_key("guessed", "VS-EVIL"), [])
    task = asyncio.create_task(rogue.run())
    try:
        assert await _until(lambda: hub.stats["auth_failures"] >= 1)
        assert not rogue.connected
        assert "VS-EVIL" not in hub.nodes
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        server.close()
        await server.wait_closed()
