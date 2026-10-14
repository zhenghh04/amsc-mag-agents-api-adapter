"""Offline check that hosted (HTTP) MCP specs are reshaped for MAG, and that
stdio MCP specs are still claimed by this adapter rather than forwarded.

No shim or network needed.

Regression: forwarding the Agents-API nested `transport` form to MAG is rejected
with `Unknown parameter: 'tools[N].transport'`.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mag_agents_shim as S  # noqa: E402

HOSTED = {"type": "mcp", "server_label": "openai_docs",
          "transport": {"type": "http", "server_url": "https://developers.openai.com/mcp"}}
STDIO = {"type": "mcp", "server_label": "local",
         "transport": {"type": "stdio", "command": "python", "args": ["-m", "srv"]}}


def main() -> int:
    ok = True

    def check(c, l):
        nonlocal ok
        ok = ok and c
        print(f"  [{'PASS' if c else 'FAIL'}] {l}")

    print("=== assertions ===")

    # Partitioning: stdio is ours, hosted is MAG's.
    check(S._is_stdio_mcp(STDIO), "stdio MCP is recognised as locally-run")
    check(not S._is_stdio_mcp(HOSTED), "hosted MCP is not treated as locally-run")
    check([s["server_label"] for s in S._parse_mcp_specs([HOSTED, STDIO])] == ["local"],
          "_parse_mcp_specs claims only the stdio server")

    # Reshaping for MAG.
    got = S._flatten_hosted_mcp([HOSTED])[0]
    check("transport" not in got, "`transport` is removed (MAG rejects the parameter)")
    check(got.get("server_url") == "https://developers.openai.com/mcp",
          "server_url is lifted to the top level")
    check(got.get("server_label") == "openai_docs", "server_label survives")
    check(got.get("require_approval") == S.MCP_REQUIRE_APPROVAL,
          f"require_approval defaults to {S.MCP_REQUIRE_APPROVAL!r} (autonomous loop)")

    # An explicit caller choice is not overridden.
    explicit = dict(HOSTED, require_approval="always")
    check(S._flatten_hosted_mcp([explicit])[0]["require_approval"] == "always",
          "caller-set require_approval is preserved")

    # Non-MCP tools pass through untouched, order preserved.
    others = [{"type": "web_search"}, HOSTED, {"type": "programmatic_tool_calling"}]
    out = S._flatten_hosted_mcp(others)
    check([t["type"] for t in out] == ["web_search", "mcp", "programmatic_tool_calling"],
          "non-MCP tools pass through, order preserved")
    check(out[0] == {"type": "web_search"}, "web_search is not mutated")

    # The input list/dicts must not be mutated in place.
    check("transport" in HOSTED, "caller's tool dict is not mutated")

    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
