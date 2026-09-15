# Examples

Runnable examples that drive the **OpenAI Agents API on MAG** through this
adapter. The Python code is **stock OpenAI SDK** — the only change versus running
against OpenAI is `OPENAI_BASE_URL`, which the runner scripts point at the local
shim.

## `codex_subagents.py` — full codex subagents on MAG

The headline example. A **coordinator** agent fans out to **three subagents in
parallel**, and *each subagent is a full codex agent with a real sandbox*
(`bash` / `fs_read` / `fs_write` / `apply_patch`). Each writes a small Python
module and actually runs it in the sandbox; the coordinator then writes a
combined `run_all.py`, runs it, and reports the real output.

This is the combination of the two starter examples, both switched on at once:

| Capability | Starter example | Here |
| --- | --- | --- |
| Real coding sandbox (`environment`) | `02_hosted_coding_stream.py` | ✅ `self_hosted` |
| Delegating to subagents (`multi_agent`) | `03_multi_agent_review.py` | ✅ `enabled` |

Under the hood the shim launches OpenAI's OSS **`codex exec-server`**, gives each
session a hardened per-session workspace (`disk-write-cwd` + read-only
elsewhere), and runs every subagent's tool calls in it concurrently — surfacing
`agent.session.subagent.{created,active,closed}` and `command_execution` items on
the event stream.

### Run it

```bash
# from the repo root; needs codex-cli, python3, and a filled-in .env
cp env.example .env          # then set MAG_API_KEY (see the README token section)
bash scripts/run_codex_subagents.sh
```

The runner boots `codex exec-server` + the shim, points the SDK at the shim, and
streams the run with a readable renderer (subagent lifecycle in magenta, sandbox
commands in blue, the final synthesis in bold).

Set `AGENTS_RAW=1` to dump the raw JSON event wire instead of the pretty view:

```bash
AGENTS_RAW=1 bash scripts/run_codex_subagents.sh
```

### What a run looks like (real output, model `gpt-5.3-codex` on MAG)

```
· session sess_1aee11a13fed41f48e9100b8
  ◆ subagent created: subagent_fib
  ▶ subagent active: subagent_fib
  ◆ subagent created: subagent_primes
  ▶ subagent active: subagent_primes
  ◆ subagent created: subagent_stats
  ▶ subagent active: subagent_stats
  ✓ subagent closed: subagent_primes
  ✓ subagent closed: subagent_fib
  ✓ subagent closed: subagent_stats
    $ cat > runner.py << 'PY'
      ...imports fib, primes, stats (the three files the subagents wrote)...
      PY
    python runner.py
      fib(10) = 55
      primes_up_to(20) = [2, 3, 5, 7, 11, 13, 17, 19]
      mean([1, 2, 3, 4]) = 2.5
      median([1, 2, 3, 4]) = 2.5
    [completed ok]

=== final answer (streaming) ===
Implemented and ran all four files successfully ...
```

Three subagents run **in parallel**, each writes and executes its own module in
the shared per-session sandbox; the coordinator then imports all three (proving
the files really landed on disk) and runs the combined program. A full captured
transcript is in the artifacts gallery as `examples/codex_subagents.sample_run.txt`.

> **MAG gotcha (real finding):** the gateway sits behind a WAF that returns
> `403` on request bodies containing injection-looking strings such as
> `python -c '...'`. Keep prompts free of inline shell one-liners and let each
> codex agent build its own `python <file>.py` command **inside** the sandbox
> (those commands go to the exec-server, not through the WAF). This example is
> written that way.

### Pick the model

The examples default to `OPENAI_AGENTS_MODEL`. On MAG, use a codex-capable
Responses model, e.g.:

```bash
export OPENAI_AGENTS_MODEL=openai/gpt-5.3-codex
```

## Rick's starter examples

The four upstream starter scripts (`01_basic_session.py`,
`02_hosted_coding_stream.py`, `03_multi_agent_review.py`,
`04_docs_mcp_research.py`, `05_raw_curl.sh`) run unchanged against the shim via
`scripts/run_examples.sh` — see that script's header for the one-time clone +
venv setup.
