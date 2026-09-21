from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent_runtime import EditingAgent, LocalEditingTools, save_outcome


def main() -> None:
    parser = argparse.ArgumentParser(description="淘宝女装 AI 智能剪辑 Agent")
    parser.add_argument("video")
    parser.add_argument("output")
    parser.add_argument("--cache-dir", default="data/cache")
    parser.add_argument("--product-type", choices=("auto", "single", "set", "bulky"), default="auto")
    parser.add_argument("--target-duration", type=float)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--report")
    args = parser.parse_args()

    tools = LocalEditingTools(cache_dir=args.cache_dir)
    agent = EditingAgent(tools, max_retries=args.max_retries)
    outcome = agent.run(
        args.video,
        args.output,
        product_type=args.product_type,
        target_duration=args.target_duration,
        progress=lambda state, payload: print(
            json.dumps({"state": state, **payload}, ensure_ascii=False), flush=True
        ),
    )
    report = Path(args.report) if args.report else Path(args.output).with_suffix(".agent.json")
    save_outcome(outcome, report)
    print(json.dumps(outcome.to_dict(), ensure_ascii=False))
    raise SystemExit(0 if outcome.status == "approved" else 2)


if __name__ == "__main__":
    main()
