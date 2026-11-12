"""HTTP 接口测试（需要 Flask；容器内已安装，本地无 Flask 时自动跳过）。

运行：python -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

try:  # pragma: no cover - 取决于运行环境
    import flask  # noqa: F401
    HAS_FLASK = True
except ImportError:  # pragma: no cover
    HAS_FLASK = False


def deep_chain_sql(layers):
    parts = ["c0 AS (SELECT mobile AS x FROM customer WHERE city = 'BJ')"]
    for i in range(1, layers + 1):
        parts.append(
            f"c{i} AS (SELECT x FROM c{i - 1} UNION ALL SELECT x FROM c{i - 1})")
    return "WITH " + ",\n".join(parts) + f"\nSELECT x FROM c{layers}"


@unittest.skipUnless(HAS_FLASK, "Flask 未安装，跳过 HTTP 接口测试")
class ServerApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server
        cls.client = server.app.test_client()

    def test_index_page_served(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("SQL".encode(), resp.data)

    def test_analyze_error_returns_400(self):
        resp = self.client.post("/api/analyze",
                                json={"schema": "", "sql": "SELECT 1"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_deep_chain_analyze_and_expand(self):
        resp = self.client.post(
            "/api/analyze",
            json={"schema": "customer(mobile*, city)", "sql": deep_chain_sql(25)})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        result = data["result"]
        # 响应中不得出现完整路径枚举
        self.assertNotIn(b"paths", resp.data)
        self.assertLess(len(resp.data), 200 * 1024)
        out = result["outputs"][0]
        self.assertEqual(
            {(d["table"], d["column"]) for d in out["value_sources"]},
            {("customer", "mobile")})
        self.assertEqual(
            {(d["table"], d["column"]) for d in out["condition_sources"]},
            {("customer", "city")})

        aid = result["analysis_id"]

        def expand(node_id, mode):
            r = self.client.post("/api/expand", json={
                "analysis_id": aid, "node_id": node_id, "mode": mode})
            self.assertEqual(r.status_code, 200)
            payload = r.get_json()
            self.assertTrue(payload["ok"])
            return payload["result"]

        # 值依赖：每个 UNION 层两个分支都能追到 customer.mobile
        frontier = [(out["node_id"], "value")]
        visited, leaves, branch_edges = set(), set(), 0
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            for edge in expand(node_id, mode)["edges"]:
                if edge["hop"].get("kind") == "union-branch":
                    branch_edges += 1
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        self.assertEqual(branch_edges, 50)
        self.assertEqual(leaves, {("customer", "mobile")})

        # 条件依赖：追到 customer.city
        frontier = [(out["node_id"], "condition")]
        visited, leaves = set(), set()
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            for edge in expand(node_id, mode)["edges"]:
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        self.assertEqual(leaves, {("customer", "city")})

    def test_expand_unknown_session_and_node(self):
        resp = self.client.post("/api/expand", json={
            "analysis_id": "nope", "node_id": "n1", "mode": "value"})
        self.assertEqual(resp.status_code, 404)

        resp = self.client.post("/api/analyze", json={
            "schema": "customer(mobile*, city)",
            "sql": "SELECT mobile FROM customer"})
        aid = resp.get_json()["result"]["analysis_id"]
        resp = self.client.post("/api/expand", json={
            "analysis_id": aid, "node_id": "nope", "mode": "value"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/expand", json={
            "analysis_id": aid, "node_id": "n1", "mode": "bogus"})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
