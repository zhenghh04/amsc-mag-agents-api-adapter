# Contributing

Thanks for helping push this adapter toward full Agents API fidelity.

## Development setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
npm install -g @openai/codex          # provides `codex exec-server`
cp env.example .env                   # set MAG_API_KEY (one unbroken line)
```

## Running the tests

```bash
bash scripts/run_all.sh               # boots exec-server + shim, runs both suites
```

Both suites must stay green:
- `tests/test_shim.py` — function-tool loop over MAG (6/6 assertions)
- `tests/test_fidelity.py` — real on-disk sandbox effect + parallel subagents

If you touch the emitted session objects or streaming events, also run the
offline SDK-contract check:

```bash
pip install 'openai==3.13.0'
python3 validate_models.py            # must be 13/13
```

And, when practical, re-run the upstream OpenAI SDK examples unchanged:

```bash
bash scripts/run_examples.sh          # see the script header for the checkout step
```

## Guidelines

- **Never commit a secret.** `.env` is gitignored; keep tokens out of code, tests,
  and scripts. Configuration flows through environment variables only.
- **Keep the SDK contract exact.** Any new event/object must validate against the
  real `openai` models (extend `validate_models.py`), not a hand-guessed shape.
- **Harden the sandbox before untrusted use.** The demos run `codex exec-server`
  with a permissive policy; production use must set a restrictive
  `sandbox_permissions` policy.
- Match the existing style: small, focused modules; no new heavy dependencies
  without discussion.

## Roadmap (the unchecked rows in the README fidelity table)

- Native streaming passthrough (forward MAG SSE + streamed tool-arg deltas)
- Context compaction via `/v1/responses/compact` when history grows
- `fs/*` sandbox ops, `apply_patch`, local MCP servers via exec-server
- Exact event-schema fidelity, auth passthrough, persistence, cancel
- Sandbox policy hardening

Please open an issue describing the change before a large PR.
