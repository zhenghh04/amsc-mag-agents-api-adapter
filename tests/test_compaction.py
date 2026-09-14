"""
Context-compaction test (deterministic, primitive-level).

Compaction's job is to bound a LONG conversation's context. We verify the
primitive end-to-end against MAG:
  1. POST /v1/responses/compact on a long synthetic conversation succeeds and
     returns fewer, reusable input items than we sent.
  2. The compacted items are valid `input` for a follow-up /v1/responses call.
  3. A fact stated EARLY in the original conversation survives compaction — the
     model answers a follow-up about it correctly from the compacted context.

This needs MAG credentials in the environment (MAG_BASE_URL / MAG_API_KEY);
the runner sets them. Run standalone with those exported.

Usage:  MAG_BASE_URL=... MAG_API_KEY=... python tests/test_compaction.py
"""
import asyncio
import os
import sys

import httpx

if not os.environ.get("MAG_BASE_URL") or not os.environ.get("MAG_API_KEY"):
    print("SKIP: MAG_BASE_URL / MAG_API_KEY not set")
    sys.exit(0)

# The shim module lives at the repo root (parent of tests/); make it importable
# regardless of the cwd the runner used.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import mag_agents_shim as shim  # imports after env is set (reads MAG_* at import)

MODEL = os.environ.get("OPENAI_AGENTS_MODEL", "openai/gpt-5.3-codex")
AUTH = f"Bearer {os.environ['MAG_API_KEY']}"

ok = True
def check(c, l):
    global ok; ok = ok and bool(c); print(f"  [{'PASS' if c else 'FAIL'}] {l}")


# A long conversation with an EARLY load-bearing fact.
big = [{"role": "user", "content": "Kickoff: the project codename is ORION-7. Acknowledge and remember it."}]
for i in range(1, 16):
    big.append({"role": "assistant", "content": f"Acknowledged note {i}; continuing planning step {i}."})
    big.append({"role": "user", "content": f"Planning detail {i}: allocate {i*3} nodes for phase {i}."})

async def main():
    async with httpx.AsyncClient(timeout=120.0) as client:
        before = shim.COMPACTIONS
        compacted = await shim._mag_compact(client, AUTH, MODEL, big)
        check(compacted is not None, "compact returned a compacted conversation")
        check(compacted is not None and len(compacted) < len(big),
              f"compacted is smaller ({len(big)} -> {len(compacted) if compacted else 'None'})")
        check(shim.COMPACTIONS == before + 1, "compaction counter incremented")
        if not compacted:
            return
        # The compacted items must be valid input to continue from.
        r = await client.post(f"{shim.MAG_BASE}/v1/responses", headers=shim._mag_headers(AUTH),
                              json={"model": MODEL,
                                    "input": compacted + [{"role": "user",
                                        "content": "From our earlier discussion, what is the project codename? Answer with just the codename."}],
                                    "store": True, "max_output_tokens": 100})
        check(r.status_code == 200, f"continue from compacted context -> {r.status_code}")
        text = shim._extract_text(r.json().get("output", [])) if r.status_code == 200 else ""
        check("ORION-7" in text or "ORION" in text,
              f"early fact survived compaction -> {text[:80]!r}")

asyncio.run(main())
print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
sys.exit(0 if ok else 1)
