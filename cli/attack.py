import asyncio
import json
import sys
import uuid
from typing import Optional
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
import websockets

from shared.schemas import AttackTrigger, AttackType, ControlAck

CONTROL_URL = "ws://localhost:9100/control"
PROXY_INGRESS_URL = "ws://localhost:8000/ocpp"

console = Console()


async def send_trigger(trigger: AttackTrigger, control_url: str = CONTROL_URL, dry_run: bool = False) -> ControlAck:
    """Send an AttackTrigger over the twin control channel, or preview if dry_run=True."""
    if dry_run:
        payload = trigger.model_dump()
        console.print(f"[bold cyan][dry-run][/bold cyan] Would dispatch trigger to [yellow]{control_url}[/yellow]: {payload}")
        return ControlAck(ok=True, detail=f"Dry-run simulated trigger: {trigger.attack_type.value}")

    try:
        async with websockets.connect(control_url) as ws:
            await ws.send(trigger.model_dump_json())
            raw_res = await asyncio.wait_for(ws.recv(), timeout=5.0)
            data = json.loads(raw_res)
            return ControlAck.model_validate(data)
    except Exception as e:
        return ControlAck(ok=False, detail=f"Failed to connect to control channel: {e}")


async def execute_session_shadow(station_id: str = "CP-01", base_url: str = PROXY_INGRESS_URL, dry_run: bool = False) -> ControlAck:
    """
    Session Shadowing Attack:
    Opens a rogue, duplicate WebSocket connection to the proxy (:8000)
    using an already-active chargePointId.
    """
    target_url = f"{base_url}/{station_id}"
    if dry_run:
        console.print(f"[bold cyan][dry-run][/bold cyan] Would initiate rogue socket connection to [yellow]{target_url}[/yellow]")
        return ControlAck(ok=True, detail=f"Dry-run simulated rogue handshake to {target_url}")

    console.print(f"[bold red][cli] Initiating rogue socket connection to {target_url}...[/bold red]")
    try:
        async with websockets.connect(target_url) as ws:
            msg_id = str(uuid.uuid4())[:8]
            shadow_frame = [
                2,
                msg_id,
                "BootNotification",
                {"chargePointVendor": "AttackerShadow", "chargePointModel": "EvilTwin-v1"},
            ]
            await ws.send(json.dumps(shadow_frame))
            res = await asyncio.wait_for(ws.recv(), timeout=4.0)
            return ControlAck(ok=True, detail=f"Rogue handshake sent to {target_url}. Response: {res}")
    except websockets.exceptions.ConnectionClosed as e:
        return ControlAck(ok=True, detail=f"Rogue socket closed by proxy (Code: {e.code}, Reason: {e.reason})")
    except Exception as e:
        return ControlAck(ok=False, detail=f"Shadow attack error: {e}")


ATTACK_VECTORS = [
    {
        "key": "1",
        "name": "Meter Spoof",
        "target": "CP-03",
        "type": AttackType.METER_SPOOF.value,
        "rule": "R7_ELECTRICAL",
        "description": "Reports 5 kW; physical V x A pack readings show ~120 kW.",
    },
    {
        "key": "2",
        "name": "Fleet Oscillate",
        "target": "Fleet-wide",
        "type": AttackType.FLEET_OSCILLATE.value,
        "rule": "R3_OSCILLATION",
        "description": "1 Hz rapid Start/Stop cycles across fleet to induce grid frequency swings.",
    },
    {
        "key": "3",
        "name": "Session Shadow",
        "target": "CP-01 (:8000)",
        "type": "session_shadow",
        "rule": "R4_SESSION_UNIQUE",
        "description": "Rogue duplicate WebSocket connection using an already-active station CPID.",
    },
    {
        "key": "4",
        "name": "Subtle Drift",
        "target": "CP-02",
        "type": AttackType.SUBTLE_DRIFT.value,
        "rule": "ML_ANOMALY",
        "description": "Compounding -2% power decay per tick designed to slip past static rules.",
    },
    {
        "key": "5",
        "name": "Card Clone",
        "target": "--station + --tag",
        "type": AttackType.TAG_CLONE.value,
        "rule": "R8_TAG_CLONE",
        "description": "One RFID card identity presented simultaneously at two bays or federated sites.",
    },
]


def get_attack_matrix() -> list[dict]:
    """Return structured list of red-team attack vectors for audit reporting."""
    return [dict(v) for v in ATTACK_VECTORS]


def display_menu():
    table = Table(title="⚡ VoltSentry Red-Team Exploit Suite ⚡", show_header=True, header_style="bold magenta")
    table.add_column("Key", style="bold cyan", width=6, justify="center")
    table.add_column("Attack Vector", style="bold white", width=20)
    table.add_column("Target", style="green", width=14)
    table.add_column("Expected Detection Rule / Mechanism", style="yellow")

    for v in ATTACK_VECTORS:
        table.add_row(v["key"], v["name"], v["target"], f"{v['rule']} ({v['description']})")
    table.add_row("r", "Fleet Reset", "All Stations", "Clears quarantine & resets to clean mode")
    table.add_row("q", "Quit", "-", "Exit Red-Team CLI")

    console.print(Panel(table, border_style="red", expand=False))


async def run_interactive_tui():
    console.clear()
    while True:
        display_menu()
        choice = click.prompt(click.style("[cli] Enter command key", bold=True, fg="bright_cyan"), default="1")
        choice = choice.strip().lower()

        if choice == "1":
            console.print("[cli] Dispatching [bold red]Meter Spoof[/bold red] to CP-03...")
            trigger = AttackTrigger(attack_type=AttackType.METER_SPOOF, station_id="CP-03")
            ack = await send_trigger(trigger, CONTROL_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice == "2":
            console.print("[cli] Dispatching [bold red]Fleet Oscillate[/bold red]...")
            trigger = AttackTrigger(attack_type=AttackType.FLEET_OSCILLATE, station_id=None)
            ack = await send_trigger(trigger, CONTROL_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice == "3":
            console.print("[cli] Executing [bold red]Session Shadowing[/bold red] directly against :8000...")
            ack = await execute_session_shadow(station_id="CP-01", base_url=PROXY_INGRESS_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice == "4":
            console.print("[cli] Dispatching [bold yellow]Subtle Drift[/bold yellow] to CP-02...")
            trigger = AttackTrigger(attack_type=AttackType.SUBTLE_DRIFT, station_id="CP-02")
            ack = await send_trigger(trigger, CONTROL_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice == "5":
            station = click.prompt("[cli] Station that presents the cloned card", default="CP-05")
            tag = click.prompt("[cli] Card to clone (idTag live elsewhere)", default="TAG-CP-03")
            console.print(f"[cli] Dispatching [bold red]Card Clone[/bold red]: {station} presents {tag}...")
            trigger = AttackTrigger(attack_type=AttackType.TAG_CLONE, station_id=station, params={"id_tag": tag})
            ack = await send_trigger(trigger, CONTROL_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice == "r":
            console.print("[cli] Dispatching [bold green]Fleet Reset[/bold green]...")
            trigger = AttackTrigger(attack_type=AttackType.RESET)
            ack = await send_trigger(trigger, CONTROL_URL)
            console.print(f"[cli] Result: {'[green]OK[/green]' if ack.ok else '[red]FAILED[/red]'} - {ack.detail}\n")

        elif choice in ("q", "quit", "exit"):
            console.print("[cli] Exiting Red-Team CLI.")
            break
        else:
            console.print(f"[cli] [red]Unknown command:[/red] {choice}\n")


@click.command()
@click.option("--attack", "-a", type=click.Choice(["1", "2", "3", "4", "5", "r"]), help="Single-shot attack command key")
@click.option("--station", "-s", default=None, help="Target station ID (e.g. CP-03)")
@click.option("--tag", "-t", default=None, help="Card to clone for attack 5 (e.g. TAG-CP-03)")
@click.option("--control-url", default=CONTROL_URL, show_default=True, help="Twin control channel of the target site")
@click.option("--proxy-url", default=PROXY_INGRESS_URL, show_default=True, help="Proxy OCPP ingress of the target site (attack 3)")
@click.option("--dry-run", is_flag=True, default=False, help="Simulate trigger and inspect payload without sending over network")
@click.option("--matrix", "-m", is_flag=True, default=False, help="Output attack vectors as JSON for audit reporting")
def main(attack: Optional[str], station: Optional[str], tag: Optional[str], control_url: str, proxy_url: str, dry_run: bool, matrix: bool):
    """VoltSentry Red-Team CLI exploit launcher."""
    global CONTROL_URL, PROXY_INGRESS_URL
    CONTROL_URL, PROXY_INGRESS_URL = control_url, proxy_url
    if matrix:
        console.print_json(json.dumps(get_attack_matrix()))
        return
    if attack:
        # Non-interactive single command mode
        if attack == "1":
            trigger = AttackTrigger(attack_type=AttackType.METER_SPOOF, station_id=station or "CP-03")
            ack = asyncio.run(send_trigger(trigger, control_url, dry_run=dry_run))
        elif attack == "2":
            trigger = AttackTrigger(attack_type=AttackType.FLEET_OSCILLATE, station_id=station)
            ack = asyncio.run(send_trigger(trigger, control_url, dry_run=dry_run))
        elif attack == "3":
            ack = asyncio.run(execute_session_shadow(station_id=station or "CP-01", base_url=proxy_url, dry_run=dry_run))
        elif attack == "4":
            trigger = AttackTrigger(attack_type=AttackType.SUBTLE_DRIFT, station_id=station or "CP-02")
            ack = asyncio.run(send_trigger(trigger, control_url, dry_run=dry_run))
        elif attack == "5":
            trigger = AttackTrigger(attack_type=AttackType.TAG_CLONE, station_id=station or "CP-05",
                                    params={"id_tag": tag or "TAG-CP-03"})
            ack = asyncio.run(send_trigger(trigger, control_url, dry_run=dry_run))
        elif attack == "r":
            trigger = AttackTrigger(attack_type=AttackType.RESET)
            ack = asyncio.run(send_trigger(trigger, control_url, dry_run=dry_run))
        console.print(f"[cli] Ack: ok={ack.ok} detail={ack.detail}")
    else:
        # Interactive TUI mode
        try:
            asyncio.run(run_interactive_tui())
        except KeyboardInterrupt:
            console.print("\n[cli] Aborted by user.")


if __name__ == "__main__":
    main()
