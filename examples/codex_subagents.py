"""Full codex subagents on MAG: a coordinator fans out to parallel subagents,
each of which does *real* work in a codex sandbox (writes files, runs code),
then the coordinator integrates their outputs and runs the combined program.

This is the combination Rick's two starter examples show separately:
  * 02_hosted_coding_stream.py  -> a real coding sandbox   (environment)
  * 03_multi_agent_review.py    -> delegating to subagents  (multi_agent)
Here both are on at once, so each subagent is a full codex agent with the
sandbox toolset (bash / fs_read / fs_write / apply_patch), running concurrently.

The SDK code is stock OpenAI — only OPENAI_BASE_URL is pointed at the MAG shim
(scripts/run_codex_subagents.sh does that). Nothing below mentions MAG.

Run:
    bash scripts/run_codex_subagents.sh
or, against an already-running shim:
    OPENAI_BASE_URL=http://127.0.0.1:8811/v1 OPENAI_API_KEY=$MAG_API_KEY \
    OPENAI_AGENTS_MODEL=openai/gpt-5.3-codex python examples/codex_subagents.py
"""
from openai import OpenAI

from common import model, print_event, require_api_key

require_api_key()

# Note: the module/command text is described in prose (no literal shell
# one-liners). The MAG gateway sits behind a WAF that flags injection-looking
# strings such as `python -c '...'`; letting each codex subagent construct its
# own `python <file>.py` command in the sandbox keeps the prompt WAF-clean.
COORDINATOR = (
    "You are a coordinator with a real code sandbox and the ability to delegate "
    "to subagents. Delegate these THREE independent tasks to THREE separate "
    "subagents, running them in parallel (call run_subagent three times in one "
    "turn). Give each subagent the full instruction for its task:\n"
    "  1. Create a file named fib.py with a function that returns the n-th "
    "Fibonacci number (the 0th is 0, the 1st is 1). Make the file print the 10th "
    "Fibonacci number when run directly, run it, and report the printed value.\n"
    "  2. Create a file named primes.py with a function that returns the list of "
    "prime numbers up to and including n. Make the file print the result for n "
    "equal to 20 when run directly, run it, and report the printed list.\n"
    "  3. Create a file named stats.py with functions for the arithmetic mean and "
    "the median of a list of numbers. Make the file print the mean and median of "
    "the numbers 1, 2, 3, 4 when run directly, run it, and report both values.\n"
    "Every subagent MUST actually create its file and run it in the sandbox with "
    "a plain command like `python fib.py` — never claim success from a completed "
    "turn alone, and do not use inline code flags. After all three subagents "
    "return, YOU create a file named run_all.py that uses all three modules and "
    "prints the three results, run it, and report the real printed output as a "
    "short labeled summary."
)


def main() -> None:
    with OpenAI() as client:
        with client.beta.agents.sessions.create(
            agent={
                "model": model(),
                "instructions": COORDINATOR,
                # Turn subagents on. Each subagent inherits the sandbox toolset.
                "multi_agent": {"enabled": True, "max_concurrent_subagents": 3},
            },
            # A real, self-hosted codex sandbox (the MAG shim runs codex exec-server).
            environment={"type": "self_hosted"},
            input="Build the three modules and the combined runner as instructed.",
            stream=True,
        ) as events:
            for event in events:
                print_event(event)


if __name__ == "__main__":
    main()
