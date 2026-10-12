"""Sequential entry point for local task capture and LLM evaluation."""
from __future__ import annotations

import argparse
from data import (capture_scenarios, run_protocol_validation, validate_instacart_data,
                  run_instacart_smoke, run_instacart_trials, summarize_real_mcp_trials)
from inference import run_experiment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("protocol", "capture", "pilot", "full",
                         "validate-data", "instacart-smoke", "instacart-trials",
                         "instacart-summary"), default="protocol")
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--faults", nargs="*", default=None)
    parser.add_argument("--allow-openai", action="store_true",
                        help="Explicitly permit new paid OpenAI calls in pilot/full modes")
    parser.add_argument("--allow-overwrite-simulator", action="store_true",
                        help="Permit regeneration of the historical simulator scenario files")
    args = parser.parse_args()
    if args.mode == "protocol":
        print(run_protocol_validation())
        return
    if args.mode == "validate-data":
        print({name: item["rows"] for name, item in validate_instacart_data().items()})
        return
    if args.mode == "instacart-smoke":
        print(run_instacart_smoke())
        return
    if args.mode == "instacart-trials":
        run_instacart_trials(args.repetitions, args.faults)
        return
    if args.mode == "instacart-summary":
        print(summarize_real_mcp_trials())
        return
    if args.mode in ("pilot", "full") and not args.allow_openai:
        parser.error("Historical OpenAI runs are complete; --allow-openai is required for new calls")
    if not args.allow_overwrite_simulator:
        parser.error("--allow-overwrite-simulator is required to regenerate historical scenario files")
    count = 60 if args.mode == "full" else 20
    scenarios = capture_scenarios(count)
    print(f"Captured {len(scenarios)} isolated MCP simulator scenarios")
    if args.mode != "capture":
        run_experiment(scenarios, mode=args.mode)


if __name__ == "__main__":
    main()
