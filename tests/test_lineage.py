"""溯源引擎测试：仅依赖 Python 标准库。

运行：python -m unittest discover -s tests -v
"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))

from lineage import analyze, analyze_with_analyzer  # noqa: E402
from lineage.errors import LineageError  # noqa: E402

SCHEMA = """
customer(cust_id, name, mobile*, id_no*, city)
orders(order_id, cust_id, amount, status, create_time)
log(id, cust_id, mobile*, event)
"""


def names(result):
    return [o["name"] for o in result["outputs"]]


def out_by_name(result, name):
    for o in result["outputs"]:
        if o["name"] == name:
            return o
    raise KeyError(name)


def dep_tables(sources):
    return {(d["table"], d["column"]) for d in sources}


def deep_chain_sql(layers):
    """c0 = SELECT mobile AS x FROM customer WHERE city='BJ'；
    之后每层 UNION ALL 两次引用上一层；最终 SELECT x。"""
    parts = ["c0 AS (SELECT mobile AS x FROM customer WHERE city = 'BJ')"]
    for i in range(1, layers + 1):
        parts.append(
            f"c{i} AS (SELECT x FROM c{i - 1} UNION ALL SELECT x FROM c{i - 1})")
    return "WITH " + ",\n".join(parts) + f"\nSELECT x FROM c{layers}"


class BasicLineageTests(unittest.TestCase):

    def test_simple_select_qualified_and_alias(self):
        r = analyze(SCHEMA, "SELECT c.cust_id AS id, c.name FROM customer AS c")
        self.assertEqual(names(r), ["id", "name"])
        col = out_by_name(r, "id")
        self.assertEqual(dep_tables(col["value_sources"]),
                         {("customer", "cust_id")})
        self.assertFalse(col["sensitive"])

    def test_value_edge_kind_via_expand(self):
        result, analyzer = analyze_with_analyzer(
            SCHEMA, "SELECT c.cust_id AS id FROM customer AS c")
        col = result["outputs"][0]
        exp = analyzer.expand(col["node_id"], "value")
        self.assertEqual(len(exp["edges"]), 1)
        edge = exp["edges"][0]
        self.assertEqual(edge["hop"]["kind"], "value")
        self.assertEqual(edge["target"]["node_type"], "base")
        self.assertEqual((edge["target"]["table"], edge["target"]["column"]),
                         ("customer", "cust_id"))

    def test_constant_column_has_no_value_dependency(self):
        r = analyze(SCHEMA, "SELECT cust_id, 'VIP' AS tag, 100 AS n FROM customer")
        tag = out_by_name(r, "tag")
        self.assertEqual(tag["value_sources"], [])
        self.assertFalse(tag["sensitive"])
        n = out_by_name(r, "n")
        self.assertEqual(n["value_sources"], [])

    def test_arithmetic_propagates_value_dependency(self):
        sql = "SELECT amount * 0.9 + 10 AS amt, -amount AS neg FROM orders"
        r = analyze(SCHEMA, sql)
        amt = out_by_name(r, "amt")
        self.assertEqual(dep_tables(amt["value_sources"]),
                         {("orders", "amount")})

    def test_case_condition_counts_as_value_dependency(self):
        sql = """
        SELECT CASE WHEN amount > 1000 THEN amount * 0.9
                    WHEN status IN ('A', 'B') THEN 0
                    ELSE amount END AS final_amt
          FROM orders
        """
        r = analyze(SCHEMA, sql)
        col = out_by_name(r, "final_amt")
        self.assertEqual(dep_tables(col["value_sources"]),
                         {("orders", "amount"), ("orders", "status")})
        self.assertFalse(col["sensitive"])

    def test_simple_case_and_like_isnull(self):
        sql = """
        SELECT CASE city WHEN 'BJ' THEN 1 ELSE 0 END AS bj,
               CASE WHEN name LIKE 'A%' THEN 1 ELSE 0 END AS a,
               CASE WHEN mobile IS NOT NULL THEN 1 ELSE 0 END AS has_m
          FROM customer
        """
        r = analyze(SCHEMA, sql)
        self.assertEqual(dep_tables(out_by_name(r, "bj")["value_sources"]),
                         {("customer", "city")})
        self.assertEqual(dep_tables(out_by_name(r, "a")["value_sources"]),
                         {("customer", "name")})
        hm = out_by_name(r, "has_m")
        self.assertEqual(dep_tables(hm["value_sources"]),
                         {("customer", "mobile")})
        self.assertIn("value", hm["sensitive_kind"])

    def test_star_expands_in_field_order(self):
        r = analyze(SCHEMA, "SELECT * FROM customer")
        self.assertEqual(names(r),
                         ["cust_id", "name", "mobile", "id_no", "city"])
        mobile = out_by_name(r, "mobile")
        self.assertTrue(mobile["from_star"])
        self.assertIn("value", mobile["sensitive_kind"])
        self.assertTrue(all(o["from_star"] for o in r["outputs"]))

    def test_table_star_expansion(self):
        sql = "SELECT c.*, o.order_id FROM customer c INNER JOIN orders o ON c.cust_id = o.cust_id"
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r),
                         ["cust_id", "name", "mobile", "id_no", "city", "order_id"])

    def test_where_applies_condition_dependency_to_all_outputs(self):
        sql = "SELECT cust_id, name FROM customer WHERE mobile IS NOT NULL"
        r = analyze(SCHEMA, sql)
        for nm in ("cust_id", "name"):
            col = out_by_name(r, nm)
            self.assertIn(("customer", "mobile"),
                          dep_tables(col["condition_sources"]))
            self.assertIn("condition", col["sensitive_kind"])
        # name 本身不引用 mobile，因此没有值依赖
        name_col = out_by_name(r, "name")
        self.assertNotIn(("customer", "mobile"),
                         dep_tables(name_col["value_sources"]))
        # 未被 WHERE 引用的列不应出现在条件来源中
        self.assertNotIn(("customer", "cust_id"),
                         dep_tables(name_col["condition_sources"]))

    def test_inner_join_on_condition_propagation(self):
        sql = """
        SELECT o.order_id, c.name
          FROM customer c
          INNER JOIN orders o ON c.cust_id = o.cust_id
         WHERE c.mobile LIKE '1%'
        """
        r = analyze(SCHEMA, sql)
        oid = out_by_name(r, "order_id")
        cond = dep_tables(oid["condition_sources"])
        self.assertIn(("customer", "cust_id"), cond)
        self.assertIn(("orders", "cust_id"), cond)
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn("condition", oid["sensitive_kind"])
        self.assertNotIn("value", oid["sensitive_kind"])

    def test_left_join_supported(self):
        sql = ("SELECT c.cust_id, o.order_id FROM customer c "
               "LEFT OUTER JOIN orders o ON c.cust_id = o.cust_id")
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["cust_id", "order_id"])

    def test_cte_chain_expands_through_cte_blocks(self):
        sql = """
        WITH c1 AS (SELECT cust_id, mobile FROM customer WHERE city = 'BJ'),
             c2 AS (SELECT cust_id, mobile FROM c1)
        SELECT cust_id, mobile FROM c2
        """
        result, analyzer = analyze_with_analyzer(SCHEMA, sql)
        mobile = out_by_name(result, "mobile")
        self.assertIn("value", mobile["sensitive_kind"])
        # 沿 value 边逐层展开，应依次经过 CTE c2、CTE c1，最终到源字段
        seen_blocks = set()
        frontier = [(mobile["node_id"], "value")]
        leaves = set()
        visited = set()
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            exp = analyzer.expand(node_id, mode)
            seen_blocks.add(exp["node"]["block"])
            for edge in exp["edges"]:
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        self.assertTrue(any("CTE c1" in b for b in seen_blocks))
        self.assertTrue(any("CTE c2" in b for b in seen_blocks))
        self.assertIn(("customer", "mobile"), leaves)
        # WHERE 中的 city 作为条件依赖贯穿到最终输出
        self.assertIn(("customer", "city"),
                      dep_tables(mobile["condition_sources"]))

    def test_cte_column_alias_list(self):
        sql = "WITH c(id, phone) AS (SELECT cust_id, mobile FROM customer) SELECT id, phone FROM c"
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["id", "phone"])
        phone = out_by_name(r, "phone")
        self.assertEqual(dep_tables(phone["value_sources"]),
                         {("customer", "mobile")})

    def test_union_all_merges_by_position(self):
        sql = """
        SELECT cust_id, mobile FROM customer
        UNION ALL
        SELECT cust_id, mobile FROM log
        """
        result, analyzer = analyze_with_analyzer(SCHEMA, sql)
        self.assertEqual(names(result), ["cust_id", "mobile"])
        mobile = out_by_name(result, "mobile")
        self.assertEqual(dep_tables(mobile["value_sources"]),
                         {("customer", "mobile"), ("log", "mobile")})
        # 最终查询本身就是 UNION：输出节点即 union 节点，两个分支各一条边
        exp = analyzer.expand(mobile["node_id"], "value")
        branches = sorted(e["hop"].get("branch") for e in exp["edges"])
        self.assertEqual(branches, [1, 2])
        self.assertTrue(all(e["hop"]["kind"] == "union-branch"
                            for e in exp["edges"]))

    def test_nested_cte_can_reference_outer(self):
        sql = """
        WITH outer_c AS (
            WITH inner_c AS (SELECT cust_id, mobile FROM customer)
            SELECT cust_id, mobile FROM inner_c
        )
        SELECT cust_id, mobile FROM outer_c
        """
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["cust_id", "mobile"])
        self.assertTrue(out_by_name(r, "mobile")["sensitive"])

    def test_field_can_be_both_value_and_condition(self):
        sql = "SELECT mobile FROM customer WHERE mobile IS NOT NULL"
        r = analyze(SCHEMA, sql)
        col = out_by_name(r, "mobile")
        self.assertEqual(sorted(col["sensitive_kind"]),
                         ["condition", "value"])

    def test_quoted_identifiers_and_comments(self):
        sql = '''
        -- 行注释
        SELECT "cust_id" AS "编号", [name] AS nm /* 块注释 */
          FROM customer
         WHERE `mobile` IS NULL
        '''
        r = analyze(SCHEMA, sql)
        self.assertEqual(names(r), ["编号", "nm"])
        nm = out_by_name(r, "nm")
        self.assertIn(("customer", "mobile"),
                      dep_tables(nm["condition_sources"]))

    def test_semicolon_allowed_at_end(self):
        r = analyze(SCHEMA, "SELECT cust_id FROM customer;")
        self.assertEqual(names(r), ["cust_id"])

    def test_cte_sensitive_overview(self):
        sql = ("WITH c AS (SELECT cust_id, mobile FROM customer) "
               "SELECT cust_id FROM c")
        r = analyze(SCHEMA, sql)
        cte_cols = {c["name"]: c for c in r["ctes"][0]["columns"]}
        self.assertTrue(cte_cols["mobile"]["sensitive"])
        self.assertFalse(cte_cols["cust_id"]["sensitive"])


class RejectionTests(unittest.TestCase):

    def assert_rejected(self, sql, schema=SCHEMA):
        with self.assertRaises(LineageError):
            analyze(schema, sql)

    def test_recursive_rejected(self):
        self.assert_rejected(
            "WITH RECURSIVE c(x) AS (SELECT 1) SELECT x FROM c")

    def test_subquery_in_from_rejected(self):
        self.assert_rejected(
            "SELECT t.cust_id FROM (SELECT cust_id FROM customer) t")

    def test_in_subquery_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id IN (SELECT cust_id FROM orders)")

    def test_function_call_rejected(self):
        self.assert_rejected("SELECT coalesce(name, 'x') FROM customer")

    def test_group_by_rejected(self):
        self.assert_rejected(
            "SELECT cust_id, count(1) FROM orders GROUP BY cust_id")

    def test_order_by_rejected(self):
        self.assert_rejected("SELECT cust_id FROM customer ORDER BY cust_id")

    def test_distinct_rejected(self):
        self.assert_rejected("SELECT DISTINCT city FROM customer")

    def test_union_without_all_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer UNION SELECT cust_id FROM log")

    def test_intersect_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer INTERSECT SELECT cust_id FROM log")

    def test_cross_join_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c CROSS JOIN orders o")

    def test_comma_join_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c, orders o WHERE c.cust_id = o.cust_id")

    def test_join_without_on_rejected(self):
        self.assert_rejected(
            "SELECT c.cust_id FROM customer c JOIN orders o")

    def test_comparison_in_output_rejected(self):
        self.assert_rejected("SELECT amount > 100 FROM orders")

    def test_string_concat_rejected(self):
        self.assert_rejected("SELECT name || city FROM customer")

    def test_between_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id BETWEEN 1 AND 9")

    def test_exists_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE EXISTS (SELECT 1)")

    def test_cast_rejected(self):
        self.assert_rejected("SELECT CAST(cust_id AS TEXT) FROM customer")

    def test_having_rejected(self):
        self.assert_rejected(
            "SELECT city FROM customer HAVING count(1) > 1")

    def test_scalar_subquery_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer WHERE cust_id = (SELECT 1)")

    def test_unclosed_string_rejected(self):
        self.assert_rejected("SELECT cust_id FROM customer WHERE name = 'x")

    def test_unknown_column_rejected(self):
        self.assert_rejected("SELECT not_a_col FROM customer")

    def test_unknown_table_rejected(self):
        self.assert_rejected("SELECT cust_id FROM nope")

    def test_ambiguous_bare_column_rejected(self):
        sql = ("SELECT cust_id FROM customer c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id")
        self.assert_rejected(sql)

    def test_unknown_qualified_column_rejected(self):
        self.assert_rejected("SELECT c.nope FROM customer c")

    def test_bad_qualifier_rejected(self):
        self.assert_rejected("SELECT x.cust_id FROM customer c")

    def test_multiple_statements_rejected(self):
        self.assert_rejected(
            "SELECT cust_id FROM customer; SELECT order_id FROM orders")

    def test_update_rejected(self):
        self.assert_rejected("UPDATE customer SET name = 'x'")

    def test_union_arity_mismatch_rejected(self):
        self.assert_rejected(
            "SELECT cust_id, name FROM customer UNION ALL SELECT cust_id FROM log")

    def test_cte_alias_arity_mismatch_rejected(self):
        self.assert_rejected(
            "WITH c(a) AS (SELECT cust_id, name FROM customer) SELECT a FROM c")

    def test_bad_schema_line_rejected(self):
        with self.assertRaises(LineageError):
            analyze("customer(cust_id, mobile*) garbage",
                    "SELECT cust_id FROM customer")

    def test_duplicate_schema_column_rejected(self):
        with self.assertRaises(LineageError):
            analyze("customer(cust_id, cust_id)",
                    "SELECT cust_id FROM customer")

    def test_empty_inputs_rejected(self):
        with self.assertRaises(LineageError):
            analyze("", "SELECT 1")
        with self.assertRaises(LineageError):
            analyze(SCHEMA, "")

    def test_modulo_rejected(self):
        self.assert_rejected("SELECT amount % 2 FROM orders")

    def test_table_star_unknown_table_rejected(self):
        self.assert_rejected("SELECT x.* FROM customer c")

    def test_no_from_constant_select(self):
        r = analyze(SCHEMA, "SELECT 1 AS x, 'y' AS y, NULL AS z")
        self.assertEqual(names(r), ["x", "y", "z"])
        for o in r["outputs"]:
            self.assertEqual(o["value_sources"], [])
            self.assertFalse(o["sensitive"])

    def test_negative_and_parenthesized_arithmetic(self):
        r = analyze(SCHEMA, "SELECT (amount + 1) * 2 - -amount AS k FROM orders")
        self.assertEqual(
            dep_tables(r["outputs"][0]["value_sources"]),
            {("orders", "amount")})

    def test_negated_predicates(self):
        sql = ("SELECT cust_id FROM customer "
               "WHERE mobile NOT IN ('1', '2') AND name NOT LIKE 'A%' "
               "AND NOT (cust_id = 1)")
        r = analyze(SCHEMA, sql)
        cond = dep_tables(r["outputs"][0]["condition_sources"])
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn(("customer", "name"), cond)
        self.assertIn(("customer", "cust_id"), cond)

    def test_join_star_expands_all_sources_in_order(self):
        schema = "t(a, b*, c)\nt2(a, d)"
        r = analyze(schema,
                    "SELECT * FROM t INNER JOIN t2 ON t.a = t2.a")
        self.assertEqual(names(r), ["a", "b", "c", "a", "d"])

    def test_duplicate_output_names_allowed(self):
        r = analyze(SCHEMA, "SELECT cust_id, cust_id FROM customer")
        self.assertEqual(names(r), ["cust_id", "cust_id"])

    def test_string_literal_with_sql_keywords_inside(self):
        r = analyze(SCHEMA, "SELECT cust_id FROM customer WHERE name = 'SELECT; FROM'")
        self.assertEqual(names(r), ["cust_id"])

    def test_inherited_condition_kept_when_downstream_has_no_where(self):
        sql = ("WITH c AS (SELECT cust_id, name FROM customer "
               "WHERE mobile IS NOT NULL) "
               "SELECT name FROM c")
        r = analyze(SCHEMA, sql)
        self.assertIn(("customer", "mobile"),
                      dep_tables(r["outputs"][0]["condition_sources"]))


class ConditionPropagationTests(unittest.TestCase):

    def test_condition_propagates_through_cte_and_union(self):
        sql = """
        WITH c AS (SELECT cust_id, name FROM customer WHERE mobile IS NOT NULL)
        SELECT cust_id, name FROM c
        UNION ALL
        SELECT cust_id, name FROM customer WHERE id_no IS NULL
        """
        r = analyze(SCHEMA, sql)
        name = out_by_name(r, "name")
        cond = dep_tables(name["condition_sources"])
        self.assertIn(("customer", "mobile"), cond)
        self.assertIn(("customer", "id_no"), cond)
        self.assertIn("condition", name["sensitive_kind"])

    def test_on_does_not_become_value_dependency(self):
        sql = ("SELECT c.name FROM customer c "
               "INNER JOIN orders o ON c.cust_id = o.cust_id")
        r = analyze(SCHEMA, sql)
        val = dep_tables(out_by_name(r, "name")["value_sources"])
        self.assertEqual(val, {("customer", "name")})

    def test_in_list_constants_only(self):
        sql = "SELECT cust_id FROM customer WHERE city IN ('BJ', 'SH') AND amount_check(cust_id) IS NULL"
        # 后半句含函数，整体应被拒
        with self.assertRaises(LineageError):
            analyze(SCHEMA, sql)
        ok = analyze(SCHEMA,
                     "SELECT cust_id FROM customer WHERE city IN ('BJ', 'SH')")
        col = out_by_name(ok, "cust_id")
        self.assertIn(("customer", "city"),
                      dep_tables(col["condition_sources"]))

    def test_condition_expansion_reaches_condition_source(self):
        sql = ("WITH c AS (SELECT cust_id, name FROM customer "
               "WHERE mobile IS NOT NULL) "
               "SELECT name FROM c")
        result, analyzer = analyze_with_analyzer(SCHEMA, sql)
        out = result["outputs"][0]
        # 条件展开应能追到 customer.mobile
        frontier = [(out["node_id"], "condition")]
        visited = set()
        leaves = set()
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            exp = analyzer.expand(node_id, mode)
            for edge in exp["edges"]:
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        self.assertIn(("customer", "mobile"), leaves)


class DeepChainTests(unittest.TestCase):
    """路径膨胀修复：每层 UNION ALL 两次引用上一层，完整路径数为 2^n。"""

    def test_25_layers_completes_quickly(self):
        sql = deep_chain_sql(25)
        start = time.time()
        result, analyzer = analyze_with_analyzer(SCHEMA, sql)
        elapsed = time.time() - start
        self.assertLess(elapsed, 5.0)
        out = result["outputs"][0]
        self.assertEqual(out["name"], "x")
        self.assertEqual(dep_tables(out["value_sources"]),
                         {("customer", "mobile")})
        self.assertEqual(dep_tables(out["condition_sources"]),
                         {("customer", "city")})
        self.assertEqual(out["sensitive_kind"], ["value"])
        # 血缘图规模与 SQL 线性相关，而非 2^25
        stats = result["graph_stats"]
        self.assertLess(stats["nodes"], 500)
        self.assertLess(stats["edges"], 1000)

    def test_response_does_not_enumerate_full_paths(self):
        for layers in (11, 25):
            result = analyze(SCHEMA, deep_chain_sql(layers))
            for out in result["outputs"]:
                self.assertNotIn("paths", out)
                for key in ("value_sources", "condition_sources"):
                    for dep in out[key]:
                        self.assertNotIn("paths", dep)
            payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
            # 11 层旧实现约 9.8MB；新实现必须保持极小且与层数近似线性
            self.assertLess(len(payload), 200 * 1024)

    def test_every_union_branch_preserved_and_traceable(self):
        layers = 25
        result, analyzer = analyze_with_analyzer(SCHEMA, deep_chain_sql(layers))
        out = result["outputs"][0]
        frontier = [(out["node_id"], "value")]
        visited = set()
        leaves = set()
        union_branch_edges = 0
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            exp = analyzer.expand(node_id, mode)
            for edge in exp["edges"]:
                if edge["hop"].get("kind") == "union-branch":
                    union_branch_edges += 1
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        # 每个 UNION 层保留 2 条分支边（c1..c25）
        self.assertEqual(union_branch_edges, layers * 2)
        self.assertEqual(leaves, {("customer", "mobile")})

    def test_duplicate_branches_to_same_upstream_kept_separate(self):
        sql = """
        WITH a AS (SELECT mobile AS x FROM customer),
             u AS (SELECT x FROM a UNION ALL SELECT x FROM a)
        SELECT x FROM u
        """
        result, analyzer = analyze_with_analyzer(SCHEMA, sql)
        out = result["outputs"][0]
        exp = analyzer.expand(out["node_id"], "value")
        union_id = exp["edges"][0]["target"]["node_id"]
        uexp = analyzer.expand(union_id, "value")
        self.assertEqual(len(uexp["edges"]), 2)
        branches = sorted(e["hop"]["branch"] for e in uexp["edges"])
        self.assertEqual(branches, [1, 2])
        # 两个分支节点各自独立，且都能继续追到同一 CTE 列
        branch_ids = {e["target"]["node_id"] for e in uexp["edges"]}
        self.assertEqual(len(branch_ids), 2)
        for e in uexp["edges"]:
            bexp = analyzer.expand(e["target"]["node_id"], "value")
            self.assertEqual(len(bexp["edges"]), 1)
            self.assertEqual(bexp["edges"][0]["target"]["node_type"], "cte")

    def test_condition_chain_through_25_layers(self):
        result, analyzer = analyze_with_analyzer(SCHEMA, deep_chain_sql(25))
        out = result["outputs"][0]
        frontier = [(out["node_id"], "condition")]
        visited = set()
        leaves = set()
        while frontier:
            node_id, mode = frontier.pop()
            if (node_id, mode) in visited:
                continue
            visited.add((node_id, mode))
            exp = analyzer.expand(node_id, mode)
            for edge in exp["edges"]:
                tgt = edge["target"]
                if tgt["is_base"]:
                    leaves.add((tgt["table"], tgt["column"]))
                else:
                    frontier.append((tgt["node_id"], edge["next_mode"]))
        self.assertEqual(leaves, {("customer", "city")})

    def test_expand_rejects_unknown_node_and_mode(self):
        _result, analyzer = analyze_with_analyzer(
            SCHEMA, "SELECT cust_id FROM customer")
        with self.assertRaises(LineageError):
            analyzer.expand("nope", "value")
        with self.assertRaises(LineageError):
            analyzer.expand("n1", "bogus")


if __name__ == "__main__":
    unittest.main(verbosity=2)
