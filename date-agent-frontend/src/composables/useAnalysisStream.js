/**
 * 统一分析流（Stage 3 新 SSE 协议）。
 *
 * 职责：POST /api/query → reader → TextDecoder → buffer → \n\n 分割
 * → 解析 data JSON → 按事件 type 回调。
 *
 * - 请求至少携带 { query, mode }；
 * - 收到 type === "done" 视为正常结束（done 是结束 loading 的正式依据）；
 * - 连接关闭但未收到 done：抛出连接异常，不得假装成功；
 * - HTTP 非 2xx（如 422）：抛出请求参数非法。
 */

export async function fetchAnalysisStream({ query, mode = "auto", onEvent, signal }) {
  const response = await fetch("/api/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query, mode }),
    signal,
  });

  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      const first = Array.isArray(body?.detail) ? body.detail[0] : null;
      detail = first?.msg || (typeof body?.detail === "string" ? body.detail : "") || "";
    } catch {
      /* 忽略解析失败 */
    }
    throw new Error(`请求参数非法（HTTP ${response.status}）${detail ? `：${detail}` : ""}`);
  }

  if (!response.body) {
    throw new Error("服务器未返回流");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let receivedDone = false;

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    let sep;
    while ((sep = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);

      const line = block.trim();
      if (!line.startsWith("data:")) continue;

      let data;
      try {
        data = JSON.parse(line.replace(/^data:\s*/, ""));
      } catch {
        continue; // 非法 JSON 块忽略，不中断流
      }

      onEvent(data);
      if (data.type === "done") {
        receivedDone = true;
      }
    }

    // done 是正常结束的正式依据：提前取消读取并结束
    if (receivedDone) {
      try {
        await reader.cancel();
      } catch {
        /* 忽略取消异常 */
      }
      break;
    }
  }

  if (!receivedDone) {
    throw new Error("连接意外中断，未收到完成事件");
  }
}
