# Developer guide — VoltSentry

Read [`AGENTS.md`](AGENTS.md) (working rules, ports, ownership) and
[`CONTEXT.md`](CONTEXT.md) (the specification) before changing code. Everything in
`AGENTS.md` applies.

## Two habits

- **Test first for pure logic.** `proxy/rules.py`, `proxy/ml_engine.py`, `proxy/ocpp.py`,
  `shared/electrical.py`, `simulator/battery.py` and `federation/protocol.py` /
  `federation/intel.py` are pure functions. Write the test, watch it fail, then make it
  pass; that loop is faster than debugging through a live socket.
- **Run it before claiming it works.** `pytest -q` for logic. For transport, start the
  stack and drive it: `python scripts/run_network.py`, then attack it with
  `python -m cli.attack` and watch `logs/<process>.log`. "Should work" is not evidence.

## Keeping things consistent

- Units live in field names (`_kw`, `_kva`, `_kwh`, `_v`, `_a`, `_hz`). Electrical
  constants live in `shared/electrical.py`; `dashboard/lib/electrical.ts` mirrors them.
- `shared/schemas.py` only gains optional fields, so the recorded replay
  (`dashboard/public/fixtures/replay.jsonl`) keeps validating. If the feed changes,
  re-record the replay from the live stack rather than editing it by hand.
- Any number quoted in the README, the landing page or `pitch/` should come from a
  measurement or a test. Update all of them together when the code changes.
