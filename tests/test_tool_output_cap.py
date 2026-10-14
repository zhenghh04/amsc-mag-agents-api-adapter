"""Offline check that oversized tool results are capped before they reach MAG.

No shim or network needed — this imports the module and exercises the cap directly.

Regression: a sandbox command emitted 11.9 MB, which went back verbatim as
function_call_output and failed the whole turn with
"Invalid 'input[0].output': string too long ... maximum length 10485760".
"""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mag_agents_shim as S  # noqa: E402


def main() -> int:
    ok = True

    def check(c, l):
        nonlocal ok
        ok = ok and c
        print(f"  [{'PASS' if c else 'FAIL'}] {l}")

    print("=== assertions ===")

    cap = S.MAX_TOOL_OUTPUT
    check(cap < 10_485_760, f"default cap {cap} is under MAG's 10485760 limit")

    short = {"type": "function_call_output", "call_id": "c1", "output": "hi"}
    check(S._cap_result(short) == short, "short output passes through untouched")

    big = "A" * 5_000_000 + "TAIL_ERROR"
    capped = S._cap_result({"type": "function_call_output", "call_id": "c2",
                            "output": big})
    out = capped["output"]
    check(len(out) <= cap + 200, f"capped {len(big)} -> {len(out)} (<= cap + marker)")
    check(out.startswith("AAA"), "head preserved")
    check(out.endswith("TAIL_ERROR"), "tail preserved (errors land at the end)")
    check("truncated by the shim" in out, "truncation is announced, not silent")
    check(capped["call_id"] == "c2" and capped["type"] == "function_call_output",
          "envelope fields survive capping")

    # Non-string outputs must not blow up the cap.
    weird = {"type": "function_call_output", "call_id": "c3", "output": None}
    check(S._cap_result(weird) == weird, "non-string output is left alone")

    # The cap is configurable.
    check(S.MAX_TOOL_OUTPUT == int(os.environ.get("SHIM_MAX_TOOL_OUTPUT", "100000")),
          "cap honours SHIM_MAX_TOOL_OUTPUT")

    print("\nRESULT:", "PASS ✅" if ok else "FAIL ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
