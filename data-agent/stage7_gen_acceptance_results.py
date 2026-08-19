"""Stage 7 收口：从 /tmp/accept_stage7/*.json 重新生成仓库根 acceptance_results.json。

读取每个原始 SSE 事件文件，输出与既有结构一致的机器可读汇总
（mode/done_status/query_count/事件计数/metric_overview/drivers/泄露扫描）。
"""

import json
import sys
from pathlib import Path

SRC_DIR = Path("/tmp/accept_stage7")
OUT = Path(__file__).resolve().parents[1] / "acceptance_results.json"

FORBIDDEN_PATTERNS = ["password", "api_key", "sk-", "traceback", "数据库密码", "Prompt"]

# 普通问数文件（mode=query）
NORMAL_FILES = [f"normal_{i}" for i in range(1, 6)]
SCENARIO1_FILES = [f"scenario1_run{i}" for i in range(1, 4)]
SCENARIO2_FILES = [f"scenario2_run{i}" for i in range(1, 4)]


def summarize(name: str) -> dict:
    events = json.loads((SRC_DIR / f"{name}.json").read_text("utf-8"))
    types: dict[str, int] = {}
    for e in events:
        types[e["type"]] = types.get(e["type"], 0) + 1
    done = next((e for e in events if e["type"] == "done"), None)
    report_event = next((e for e in events if e["type"] == "report"), None)
    report = report_event.get("report") if report_event else None
    raw_text = json.dumps(events, ensure_ascii=False)
    forbidden = sum(1 for p in FORBIDDEN_PATTERNS if p.lower() in raw_text.lower())

    mode = "query"
    if name in SCENARIO1_FILES or name in SCENARIO2_FILES:
        mode = "attribution"

    summary = {
        "file": f"{name}.json",
        "mode": mode,
        "done_status": done.get("status") if done else None,
        "query_count": done.get("query_count") if done else None,
        "n_action_start": types.get("action_start", 0),
        "n_query_result": types.get("query_result", 0),
        "n_calculation": types.get("calculation", 0),
        "n_report": types.get("report", 0),
        "n_error": types.get("error", 0),
        "event_types": sorted(types),
        "has_report": report is not None,
        "metric_overview": report.get("metric_overview", []) if report else [],
        "drivers": report.get("drivers", []) if report else [],
        "forbidden_hits": forbidden,
    }
    return summary


def main() -> int:
    runs = []
    for name in NORMAL_FILES + SCENARIO1_FILES + SCENARIO2_FILES:
        src = SRC_DIR / f"{name}.json"
        if not src.exists():
            print(f"missing: {src}", file=sys.stderr)
            return 1
        runs.append(summarize(name))

    payload = {
        "generated_by": "Stage 7 最终收口验收（POST /api/query 真实 API/SSE）",
        "note": "机器可读汇总（仓库根目录 acceptance_results.json）；原始事件存于 /tmp/accept_stage7/*.json。",
        "runs": runs,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    print(f"wrote {OUT} ({len(runs)} runs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
