import argparse
import asyncio
import json
from datetime import datetime, timezone
import websockets

transaction_counter = 1000


async def handle_client(websocket):
    global transaction_counter
    prefix = "[csms]"
    print(f"{prefix} New connection accepted", flush=True)
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue

            if not isinstance(data, list) or len(data) < 4 or data[0] != 2:
                continue

            msg_type, msg_id, action, payload = data[0], data[1], data[2], data[3]
            res_payload = {}

            if action == "BootNotification":
                res_payload = {
                    "status": "Accepted",
                    "currentTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "interval": 10,
                }
            elif action == "Authorize":
                res_payload = {"idTagInfo": {"status": "Accepted"}}
            elif action == "StartTransaction":
                transaction_counter += 1
                res_payload = {
                    "transactionId": transaction_counter,
                    "idTagInfo": {"status": "Accepted"},
                }
            elif action == "MeterValues":
                res_payload = {}
            elif action == "StopTransaction":
                res_payload = {"idTagInfo": {"status": "Accepted"}}

            response = [3, msg_id, res_payload]
            await websocket.send(json.dumps(response))
            print(f"{prefix} Handled {action} -> CALLRESULT [3, {msg_id}]", flush=True)
    except websockets.exceptions.ConnectionClosed:
        print(f"{prefix} Connection closed", flush=True)


async def main():
    # One CSMS serves every site, as an operator's central system does; each
    # site's VoltSentry node dials it. Transaction ids are unique network-wide.
    p = argparse.ArgumentParser(description="Mock central CSMS")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=9000)
    a = p.parse_args()
    print(f"[csms] Listening for OCPP connections on ws://{a.host}:{a.port}", flush=True)
    async with websockets.serve(handle_client, a.host, a.port):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
