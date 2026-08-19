"""Stage 7 最终收口验收：真实 API/SSE 回归脚本。

执行 5 个普通问数 + 两个冻结归因场景各 3 次，保存原始 SSE 事件到
`/tmp/accept_stage7/<name>.json`，并输出机器可读汇总（done 状态、
事件计数、冻结数值核对、敏感信息泄露扫描）。

用法（需先启动后端）：
    uv run python main.py          # 终端 A
    uv run python stage7_accept_api.py   # 终端 B

本脚本只做验收，不进生产调用路径。
"""

import json
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/query"
OUT_DIR = Path("/tmp/accept_stage7")
OUT_DIR.mkdir(parents=True, exist_ok=True)

NORMAL_QUERIES = [
    ("normal_1", "统计2025年各月销售额", "query"),
    ("normal_2", "统计各个地区的销售额", "query"),
    ("normal_3", "统计各产品类别的销售数量", "query"),
    ("normal_4", "统计2025年2月各产品的销售额", "query"),
    ("normal_5", "统计黄金等级客户的销售额", "query"),
]

SCENARIO_1 = "为什么2025年2月销售额较1月明显下降？"
SCENARIO_2 = "为什么2025年3月销售数量大幅增长，但销售额增长有限？"

FORBIDDEN_PATTERNS = ["password", "api_key", "sk-", "traceback", "数据库密码", "Prompt"]


def run_query(name: str, query: str, mode: str) -> list[dict]:
    payload = json.dumps({"query": query, "mode": mode}).encode("utf-8")
    req = urllib.request.Request(
        BASE, data=payload, headers={"Content-Type": "application/json"}
    )
    events: list[dict] = []
    with urllib.request.urlopen(req, timeout=900) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[len("data: "):].strip()))
                except json.JSONDecodeError:
                    continue
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(events, ensure_ascii=False, indent=2), "utf-8"
    )
    return events


def summarize(name: str, events: list[dict]) -> dict:
    types: dict[str, int] = {}
    for e in events:
        types[e["type"]] = types.get(e["type"], 0) + 1

    done = next((e for e in events if e["type"] == "done"), None)
    report = next((e for e in events if e["type"] == "report"), None)
    report_payload = report.get("report") if report else None

    raw_text = json.dumps(events, ensure_ascii=False)
    forbidden_hits = sum(1 for p in FORBIDDEN_PATTERNS if p.lower() in raw_text.lower())

    summary = {
        "file": f"{name}.json",
        "done_status": done.get("status") if done else None,
        "query_count": done.get("query_count") if done else None,
        "n_action_start": types.get("action_start", 0),
        "n_query_result": types.get("query_result", 0),
        "n_calculation": types.get("calculation", 0),
        "n_report": types.get("report", 0),
        "n_error": types.get("error", 0),
        "event_types": sorted(types),
        "forbidden_hits": forbidden_hits,
    }

    if report_payload:
        summary["metric_overview"] = report_payload.get("metric_overview", [])
        summary["n_drivers"] = len(report_payload.get("drivers", []))
        summary["n_offsets"] = len(report_payload.get("offsets", []))
    return summary


def print_summary(name: str, s: dict) -> None:
    print(f"[{name}] done={s['done_status']} qcount={s['query_count']} "
          f"action_start={s['n_action_start']} query_result={s['n_query_result']} "
          f"calculation={s['n_calculation']} report={s['n_report']} error={s['n_error']} "
          f"forbidden={s['forbidden_hits']}")
    for mo in s.get("metric_overview", []):
        print(f"    overview[{mo['metric']}]: {mo['comparison_value']} -> {mo['current_value']} "
              f"delta={mo.get('delta')} rate={mo.get('change_rate')}")
    if "n_drivers" in s:
        print(f"    drivers={s['n_drivers']} offsets={s['n_offsets']}")


def main() -> int:
    results: list[dict] = []
    fail = 0

    for name, query, mode in NORMAL_QUERIES:
        events = run_query(name, query, mode)
        s = summarize(name, events)
        results.append(s)
        print_summary(name, s)
        if s["done_status"] != "completed" or s["n_error"] != 0:
            fail += 1

    for i in range(1, 4):
        name = f"scenario1_run{i}"
        events = run_query(name, SCENARIO_1, "auto")
        s = summarize(name, events)
        results.append(s)
        print_summary(name, s)
        if s["done_status"] != "completed" or s["n_error"] != 0:
            fail += 1

    for i in range(1, 4):
        name = f"scenario2_run{i}"
        events = run_query(name, SCENARIO_2, "auto")
        s = summarize(name, events)
        results.append(s)
        print_summary(name, s)
        if s["done_status"] != "completed" or s["n_error"] != 0:
            fail += 1

    (OUT_DIR / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), "utf-8"
    )
    print(f"\n=== TOTAL: {len(results)} runs, failures={fail} ===")
    print(f"events saved to {OUT_DIR}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
