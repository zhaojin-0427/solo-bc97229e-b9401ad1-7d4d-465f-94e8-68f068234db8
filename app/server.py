"""Flask Web 入口：溯源页面 + JSON 接口。

接口：
- POST /api/analyze  返回源字段摘要（不枚举完整路径），并登记一次分析会话
- POST /api/expand   按用户点击逐层返回某节点的直接上游（共享血缘图的一跳）
"""

import os
import sys
import threading
import uuid
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, render_template, request

from lineage import analyze_with_analyzer
from lineage.demo_data import DEMO_SCHEMA, DEMO_SQL
from lineage.errors import LineageError

app = Flask(__name__)

# 分析会话：analysis_id -> (result, analyzer)。血缘图只读，线程安全。
_MAX_SESSIONS = 32
_sessions = OrderedDict()
_sessions_lock = threading.Lock()


def _remember(result, analyzer):
    analysis_id = uuid.uuid4().hex
    with _sessions_lock:
        _sessions[analysis_id] = (result, analyzer)
        while len(_sessions) > _MAX_SESSIONS:
            _sessions.popitem(last=False)
    return analysis_id


def _lookup(analysis_id):
    with _sessions_lock:
        return _sessions.get(analysis_id)


@app.route("/")
def index():
    return render_template("index.html",
                           demo_schema=DEMO_SCHEMA,
                           demo_sql=DEMO_SQL)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    payload = request.get_json(silent=True) or {}
    schema_text = payload.get("schema", "")
    sql_text = payload.get("sql", "")
    try:
        result, analyzer = analyze_with_analyzer(schema_text, sql_text)
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return jsonify({"ok": False,
                        "error": f"分析失败：{type(exc).__name__}: {exc}"}), 400
    result["analysis_id"] = _remember(result, analyzer)
    return jsonify({"ok": True, "result": result})


@app.route("/api/expand", methods=["POST"])
def api_expand():
    payload = request.get_json(silent=True) or {}
    analysis_id = payload.get("analysis_id", "")
    node_id = payload.get("node_id", "")
    mode = payload.get("mode", "value")

    entry = _lookup(analysis_id)
    if entry is None:
        return jsonify({"ok": False,
                        "error": "分析会话不存在或已过期，请重新执行分析"}), 404
    _result, analyzer = entry
    try:
        expansion = analyzer.expand(node_id, mode)
    except LineageError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - 防御性兜底
        return jsonify({"ok": False,
                        "error": f"展开失败：{type(exc).__name__}: {exc}"}), 400
    return jsonify({"ok": True, "result": expansion})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), debug=False)
