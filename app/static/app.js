/* 溯源结果渲染（原生 JS，无外部依赖）。
 *
 * 初始渲染只展示“源字段摘要”（值来源 / 条件来源），不渲染任何完整路径；
 * 用户点击“展开”后，通过 /api/expand 逐层获取直接上游，任一分支都能
 * 继续展开直到源字段。
 */

const $ = (id) => document.getElementById(id);

let currentAnalysisId = null;
let boxSeq = 0;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function sensitiveClass(kind) {
  if (kind.includes("value") && kind.includes("condition")) return "tag-both";
  if (kind.includes("value")) return "tag-value";
  if (kind.includes("condition")) return "tag-cond";
  return "tag-clean";
}

function sensitiveLabel(kind) {
  if (kind.includes("value") && kind.includes("condition")) return "值+条件敏感";
  if (kind.includes("value")) return "值依赖敏感";
  if (kind.includes("condition")) return "条件依赖敏感";
  return "无敏感来源";
}

function hopText(hop) {
  const parts = [];
  parts.push(hop.block || "");
  if (hop.clause) parts.push(hop.clause);
  if (hop.ref) parts.push(hop.ref);
  parts.push("→ " + hop.name);
  if (hop.branch) parts.push(`(分支${hop.branch})`);
  return parts.filter(Boolean).join(" ");
}

function sourceChip(dep) {
  return `<span class="src-chip ${dep.sensitive ? "sensitive" : ""}">` +
    `${esc(dep.table)}.${esc(dep.column)}${dep.sensitive ? " 🔒" : ""}</span>`;
}

function renderSourceSummary(title, cls, sources, emptyText) {
  const body = sources.length
    ? sources.map(sourceChip).join("")
    : `<span class="empty-note">${esc(emptyText)}</span>`;
  return `<div class="dep-group">
    <div class="dep-title ${sources.some((d) => d.sensitive) ? cls : "clean"}">
      ${esc(title)}（${sources.length} 个源字段）
    </div>
    <div class="src-chips">${body}</div>
  </div>`;
}

function nodeHeadLine(node) {
  const bits = [];
  bits.push(`<span class="tree-block">${esc(node.block || "")}</span>`);
  bits.push(`<span class="tree-name">${esc(node.name)}</span>`);
  bits.push(`<span class="tag ${sensitiveClass(node.sensitive_kind)}">${esc(sensitiveLabel(node.sensitive_kind))}</span>`);
  if (node.expr_text) bits.push(`<span class="tree-expr">${esc(node.expr_text)}</span>`);
  return bits.join(" ");
}

function targetLabel(t) {
  if (t.is_base) return `${t.table}.${t.column}`;
  return `${t.block} · ${t.name}`;
}

function renderExpansion(res) {
  const node = res.node;
  const head = `<div class="tree-head">${nodeHeadLine(node)}</div>`;
  if (!res.edges.length) {
    return head + '<div class="empty-note">（无直接上游）</div>';
  }
  const rows = res.edges.map((e) => {
    const t = e.target;
    const boxId = `box-${++boxSeq}`;
    let badge;
    if (e.as_condition) {
      badge = '<span class="tag tag-cond">条件传播</span>';
    } else if (e.kind === "condition") {
      badge = '<span class="tag tag-cond">条件</span>';
    } else {
      badge = '<span class="tag tag-value">值</span>';
    }
    let tail;
    if (t.is_base) {
      tail = `<span class="src-leaf ${t.sensitive ? "sensitive" : ""}">` +
        `源字段 ${esc(t.table)}.${esc(t.column)}${t.sensitive ? " 🔒" : ""}</span>`;
    } else {
      tail = `<button type="button" class="link" data-expand ` +
        `data-node="${esc(t.node_id)}" data-mode="${esc(e.next_mode)}" ` +
        `data-box="${boxId}">展开下一层</button>`;
    }
    const child = t.is_base ? "" : `<div class="tree-child" id="${boxId}"></div>`;
    return `<div class="edge-item"><div class="edge-row">${badge}` +
      `<span class="hop kind-${esc(e.hop.kind)}">${esc(hopText(e.hop))}</span>` +
      `<span class="arrow">→</span>` +
      `<span class="target">${esc(targetLabel(t))}</span>${tail}</div>${child}</div>`;
  }).join("");
  return head + `<div class="edge-list">${rows}</div>`;
}

async function expandInto(box, nodeId, mode, btn) {
  box.innerHTML = '<div class="empty-note">加载中…</div>';
  const fail = (msg) => {
    box.innerHTML = `<div class="error-box">${esc(msg)}</div>`;
    delete box.dataset.loaded;  // 允许重试
    if (btn) btn.textContent = btn.dataset.openLabel || btn.textContent;
  };
  try {
    const resp = await fetch("/api/expand", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        analysis_id: currentAnalysisId,
        node_id: nodeId,
        mode,
      }),
    });
    const data = await resp.json();
    if (!data.ok) {
      fail(data.error);
      return;
    }
    box.innerHTML = renderExpansion(data.result);
    if (btn) btn.textContent = "收起";
  } catch (e) {
    fail("请求失败：" + e);
  }
}

function renderResult(data) {
  const { result } = data;
  currentAnalysisId = result.analysis_id;
  const html = [];

  // 源表概览
  html.push('<div class="section-title">源表与敏感字段</div>');
  html.push('<div class="meta-tables">');
  for (const t of result.base_tables) {
    const cols = t.columns.map((c) =>
      `<span class="meta-col ${c.sensitive ? "sensitive" : ""}">${esc(c.name)}${c.sensitive ? " *" : ""}</span>`
    ).join("");
    html.push(`<div class="meta-table"><b>${esc(t.name)}</b><div>${cols}</div></div>`);
  }
  html.push("</div>");

  // CTE 概览
  if (result.ctes.length) {
    html.push('<div class="section-title">CTE 列敏感性概览</div>');
    for (const cte of result.ctes) {
      const cols = cte.columns.map((c) => {
        const cls = c.sensitive_kind.includes("value") && c.sensitive_kind.includes("condition")
          ? "s-both" : c.sensitive_kind.includes("value")
          ? "s-value" : c.sensitive_kind.includes("condition")
          ? "s-condition" : "";
        return `<span class="cte-col ${cls}">${esc(c.name)}${c.sensitive ? " 🔒" : ""}</span>`;
      }).join("");
      html.push(`<div class="cte-block"><div class="cte-name">WITH ${esc(cte.name)}</div>
        <div class="cte-cols">${cols}</div></div>`);
    }
  }

  // 逐输出列：先源字段摘要，再按需逐层展开
  html.push('<div class="section-title">最终输出列逐列溯源（先摘要，点击逐层展开）</div>');
  for (const col of result.outputs) {
    const kind = col.sensitive_kind;
    const vBox = `box-${++boxSeq}`;
    const cBox = `box-${++boxSeq}`;
    html.push(`
    <div class="out-card">
      <div class="out-head">
        <span class="out-pos">${col.position}</span>
        <span class="out-name">${esc(col.name)}</span>
        <span class="tag ${sensitiveClass(kind)}">${esc(sensitiveLabel(kind))}</span>
        ${col.from_star ? '<span class="tag tag-clean">星号展开</span>' : ""}
        <span class="out-expr">${esc(col.expr_text || "")}</span>
      </div>
      <div class="out-body">
        ${renderSourceSummary("值来源（输出表达式 / CASE 条件实际引用）", "value",
          col.value_sources, "常量表达式，无源字段值依赖")}
        ${renderSourceSummary("条件来源（本块及沿途 WHERE / JOIN ON）", "condition",
          col.condition_sources, "无 WHERE / ON 条件依赖")}
        <div class="expand-actions">
          <button type="button" class="link" data-expand data-node="${esc(col.node_id)}"
            data-mode="value" data-box="${vBox}">展开值依赖</button>
          <button type="button" class="link" data-expand data-node="${esc(col.node_id)}"
            data-mode="condition" data-box="${cBox}">展开条件依赖</button>
        </div>
        <div class="tree-child" id="${vBox}"></div>
        <div class="tree-child" id="${cBox}"></div>
      </div>
    </div>`);
  }
  $("result").innerHTML = html.join("");
}

async function runAnalyze() {
  const status = $("status");
  const errBox = $("error");
  errBox.hidden = true;
  status.textContent = "分析中…";
  try {
    const resp = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ schema: $("schema").value, sql: $("sql").value }),
    });
    const data = await resp.json();
    if (!data.ok) {
      errBox.textContent = "语法/溯源错误（已拒绝，未执行任何 SQL）：\n" + data.error;
      errBox.hidden = false;
      $("result").innerHTML = "";
      status.textContent = "失败";
      return;
    }
    renderResult(data);
    const n = data.result.outputs.length;
    const sens = data.result.outputs.filter((o) => o.sensitive).length;
    const stats = data.result.graph_stats || { nodes: 0, edges: 0 };
    status.textContent =
      `完成：${n} 个输出列，其中 ${sens} 个含敏感来源；` +
      `血缘图 ${stats.nodes} 节点 / ${stats.edges} 边（共享结构，未枚举完整路径）`;
  } catch (e) {
    errBox.textContent = "请求失败：" + e;
    errBox.hidden = false;
    status.textContent = "失败";
  }
}

// 事件委托：所有“展开/收起”按钮
$("result").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-expand]");
  if (!btn) return;
  const box = document.getElementById(btn.dataset.box);
  if (!box) return;
  if (box.dataset.loaded === "1") {
    const hidden = box.classList.toggle("collapsed");
    btn.textContent = hidden ? btn.dataset.openLabel : "收起";
    return;
  }
  btn.dataset.openLabel = btn.textContent;
  box.dataset.loaded = "1";
  expandInto(box, btn.dataset.node, btn.dataset.mode, btn);
});

$("run").addEventListener("click", runAnalyze);
$("reset").addEventListener("click", () => {
  $("schema").value = window.DEMO_SCHEMA;
  $("sql").value = window.DEMO_SQL;
  $("error").hidden = true;
  $("status").textContent = "";
  $("result").innerHTML = "";
  currentAnalysisId = null;
});

// 25 层 UNION ALL 路径膨胀示例：旧实现 11 层即约 9.8MB，本版 25 层毫秒级完成
function deepChainSql(layers) {
  const parts = ["  c0 AS (SELECT mobile AS x FROM customer WHERE city = 'BJ')"];
  for (let i = 1; i <= layers; i++) {
    parts.push(`  c${i} AS (SELECT x FROM c${i - 1} UNION ALL SELECT x FROM c${i - 1})`);
  }
  return "WITH\n" + parts.join(",\n") + `\nSELECT x FROM c${layers};`;
}

$("deep-demo").addEventListener("click", () => {
  $("schema").value = "customer(mobile*, city)";
  $("sql").value = deepChainSql(25);
  $("error").hidden = true;
  runAnalyze();
});

// 支持 Ctrl/Cmd+Enter 触发
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") runAnalyze();
});

// 首次加载自动分析示例
window.addEventListener("DOMContentLoaded", runAnalyze);
