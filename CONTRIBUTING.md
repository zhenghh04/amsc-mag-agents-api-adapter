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
bash scripts/run_all.sh               # boots hardened exec-server + shim, runs all suites
```

All four suites must stay green:
- `tests/test_shim.py` — function-tool loop over MAG
- `tests/test_fidelity.py` — real on-disk sandbox effect + parallel subagents
- `tests/test_next_steps.py` — streaming deltas, apply_patch, auth passthrough, persistence, cancel
- `tests/test_compaction.py` — `/v1/responses/compact` (early fact survives, compacted result reusable)

If you touch the emitted session objects or streaming events, also run the
offline SDK-contract check:

```bash
pip install 'openai==3.13.0'
python3 validate_models.py            # must be 22/22
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
- **Keep the sandbox hardened.** `codex exec-server` is launched with
  `disk-write-cwd` + `disk-full-read-access` and each session writes only to its
  own workspace. Don't regress to a permissive policy; tighten further for your
  threat model.
- Match the existing style: small, focused modules; no new heavy dependencies
  without discussion.

## Roadmap (remaining unchecked rows in the README fidelity table)

- Local MCP servers exposed through `codex exec-server` to the agent loop
- Multi-user-turn session resume (`GET` then continue) so compaction spans turns
- OpenAI-hosted `environment` type (managed container) parity

Done since the initial spike: native streaming passthrough, context compaction,
`apply_patch`/`fs` tools, richer event schema, auth passthrough, persistence,
cancel, and sandbox hardening.

Please open an issue describing the change before a large PR.
