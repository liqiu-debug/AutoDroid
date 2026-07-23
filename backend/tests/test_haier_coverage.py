import io
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from backend.inspection.haier_coverage import (
    CoverageAuditError,
    HAIER_COVERAGE_MANIFEST,
    ManifestItem,
    PHYSICAL_PRODUCT_DETAIL,
    StateEvidence,
    StateMatcher,
    SERVICE_DETAIL,
    TransitionEvidence,
    audit_haier_coverage,
    evaluate_manifest,
    weighted_coverage,
)
from scripts.maintenance.audit_haier_coverage import main as coverage_cli_main


def _state(state_id, subtype, *, xml=None, branch=1):
    return StateEvidence(
        id=state_id,
        run_id=7,
        branch_run_id=branch,
        page_subtype=subtype,
        xml_text=xml,
    )


def _edge(edge_id, source, target, *, role="COMMAND", status="PASS", branch=1):
    return TransitionEvidence(
        id=edge_id,
        run_id=7,
        branch_run_id=branch,
        from_state_id=source,
        to_state_id=target,
        action_role=role,
        execution_disposition="EXECUTED",
        status=status,
    )


class HaierCoveragePureTests(unittest.TestCase):
    def test_generic_goods_after_sales_copy_is_not_service_detail(self):
        goods = _state(
            1,
            "PRODUCT_DETAIL",
            xml="Haier/海尔 一级能耗 商品参数 上门服务",
        )
        service = _state(
            2,
            "SERVICE_DETAIL",
            xml="家生活服务 深度清洗 原厂服务",
        )

        self.assertTrue(PHYSICAL_PRODUCT_DETAIL.matches(goods))
        self.assertFalse(SERVICE_DETAIL.matches(goods))
        self.assertTrue(SERVICE_DETAIL.matches(service))
        self.assertFalse(PHYSICAL_PRODUCT_DETAIL.matches(service))

    def test_missing_manifest_item_stays_in_weighted_denominator(self):
        manifest = (
            ManifestItem("home", "home", 1.0, True, "state", StateMatcher(("HOME",))),
            ManifestItem("orders", "orders", 3.0, False, "state", StateMatcher(("ORDER",))),
        )

        results = evaluate_manifest([_state(1, "HOME")], [], manifest)

        self.assertTrue(results[0].covered)
        self.assertFalse(results[1].covered)
        self.assertEqual(weighted_coverage(results), 0.25)

    def test_path_item_needs_real_transition_chain_not_subtype_presence(self):
        manifest = (
            ManifestItem(
                "flow",
                "flow",
                1.0,
                True,
                "path",
                path=(StateMatcher(("PROFILE",)), StateMatcher(("ORDER",))),
            ),
        )
        states = [_state(1, "PROFILE"), _state(2, "ORDER")]

        without_edge = evaluate_manifest(states, [], manifest)[0]
        failed_edge = evaluate_manifest(
            states,
            [_edge(10, 1, 2, status="LOCATOR_NOT_FOUND")],
            manifest,
        )[0]
        with_edge = evaluate_manifest(states, [_edge(11, 1, 2)], manifest)[0]

        self.assertFalse(without_edge.covered)
        self.assertFalse(failed_edge.covered)
        self.assertTrue(with_edge.covered)
        self.assertEqual(with_edge.evidence_state_ids, (1, 2))
        self.assertEqual(with_edge.evidence_transition_ids, (11,))

    def test_bottom_tabs_reject_cross_branch_transition(self):
        item = next(
            row for row in HAIER_COVERAGE_MANIFEST if row.key == "home_five_tabs"
        )
        states = [
            _state(1, "HOME", xml="首页 分类 许愿池 购物车 我的"),
            _state(2, "CATALOG_CATEGORY"),
            _state(3, "COMMUNITY_FEED"),
            _state(4, "CART"),
            _state(5, "PROFILE"),
        ]
        edges = [
            _edge(10, 1, 2),
            _edge(11, 1, 3),
            _edge(12, 1, 4),
            _edge(13, 1, 5, branch=2),
        ]

        result = evaluate_manifest(states, edges, (item,))[0]

        self.assertFalse(result.covered)

    def test_payment_path_requires_full_chain_and_blocked_boundary(self):
        item = next(
            row
            for row in HAIER_COVERAGE_MANIFEST
            if row.key == "physical_checkout_safety_flow"
        )
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 一级能耗 商品参数"),
            _state(2, "PURCHASE_OPTIONS", xml="已选 1件"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        chain = [
            _edge(10, 1, 2, role="BUY_NOW"),
            _edge(11, 2, 3, role="CHECKOUT"),
            _edge(12, 3, 4, role="PLACE_ORDER"),
        ]

        reached_only = evaluate_manifest(states, chain, (item,))[0]
        blocked = TransitionEvidence(
            id=13,
            run_id=7,
            branch_run_id=1,
            from_state_id=4,
            to_state_id=None,
            action_role="COMMAND:PAY",
            execution_disposition="SKIPPED",
            status="BLOCKED",
            failure_type="SAFETY_BLOCKED",
            risk_type="PAYMENT",
        )
        wrong_branch = TransitionEvidence(
            id=14,
            run_id=7,
            branch_run_id=2,
            from_state_id=4,
            to_state_id=None,
            execution_disposition="SKIPPED",
            status="BLOCKED",
            failure_type="SAFETY_BLOCKED",
            risk_type="PAYMENT",
        )
        cross_branch = evaluate_manifest(states, [*chain, wrong_branch], (item,))[0]
        accepted = evaluate_manifest(states, [*chain, blocked], (item,))[0]

        self.assertFalse(reached_only.covered)
        self.assertFalse(cross_branch.covered)
        self.assertEqual(reached_only.evidence_state_ids, (1, 2, 3, 4))
        self.assertTrue(accepted.covered)
        self.assertEqual(accepted.evidence_transition_ids, (10, 11, 12, 13))

    def test_payment_path_accepts_inline_selected_specification(self):
        item = next(
            row
            for row in HAIER_COVERAGE_MANIFEST
            if row.key == "physical_checkout_safety_flow"
        )
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 一级能耗 已选 BCD-336"),
            _state(2, "CHECKOUT", xml="提交订单"),
            _state(3, "CASHIER", xml="海尔收银台"),
        ]
        transitions = [
            _edge(10, 1, 2, role="BUY_NOW"),
            _edge(11, 2, 3, role="PLACE_ORDER"),
            TransitionEvidence(
                id=12,
                run_id=7,
                branch_run_id=1,
                from_state_id=3,
                to_state_id=None,
                execution_disposition="EXECUTED",
                status="BLOCKED",
                risk_type="PAYMENT",
            ),
        ]

        result = evaluate_manifest(states, transitions, (item,))[0]

        self.assertTrue(result.covered)
        self.assertEqual(result.evidence_state_ids, (1, 2, 3))
        self.assertIn("inline selected specification", result.detail)

    def test_custom_payment_manifest_does_not_inherit_haier_alternative(self):
        custom = ManifestItem(
            "custom_payment",
            "custom payment",
            1.0,
            True,
            "payment_safety_path",
            path=(StateMatcher(("NEVER",)), StateMatcher(("CASHIER",))),
        )
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 一级能耗 已选 BCD-336"),
            _state(2, "CHECKOUT", xml="提交订单"),
            _state(3, "CASHIER", xml="海尔收银台"),
        ]
        transitions = [
            _edge(10, 1, 2, role="BUY_NOW"),
            _edge(11, 2, 3, role="PLACE_ORDER"),
        ]

        result = evaluate_manifest(states, transitions, (custom,))[0]

        self.assertFalse(result.covered)

    def test_service_detail_cannot_satisfy_physical_purchase_matcher(self):
        item = next(
            row
            for row in HAIER_COVERAGE_MANIFEST
            if row.key == "physical_checkout_safety_flow"
        )
        states = [
            _state(1, "PRODUCT_DETAIL", xml="Haier/海尔 原厂服务 深度清洗 正品保障"),
            _state(2, "PURCHASE_OPTIONS"),
            _state(3, "CHECKOUT", xml="提交订单"),
            _state(4, "CASHIER", xml="海尔收银台"),
        ]
        transitions = [
            _edge(10, 1, 2, role="BUY_NOW"),
            _edge(11, 2, 3, role="BUY_NOW"),
            _edge(12, 3, 4, role="PLACE_ORDER"),
            TransitionEvidence(
                id=13,
                run_id=7,
                branch_run_id=1,
                from_state_id=4,
                to_state_id=None,
                execution_disposition="EXECUTED",
                status="BLOCKED",
                risk_type="PAYMENT",
            ),
        ]

        result = evaluate_manifest(states, transitions, (item,))[0]

        self.assertFalse(result.covered)

    def test_service_detail_flow_accepts_dedicated_service_subtype(self):
        item = next(
            row
            for row in HAIER_COVERAGE_MANIFEST
            if row.key == "service_detail_flow"
        )
        states = [
            _state(1, "SERVICE_LIST", xml="家电维修 原厂服务"),
            _state(2, "SERVICE_DETAIL", xml="家生活服务 深度清洗"),
        ]

        result = evaluate_manifest(
            states,
            [_edge(10, 1, 2, role="ITEM_OPEN:collection")],
            (item,),
        )[0]

        self.assertTrue(result.covered)
        self.assertEqual(result.evidence_state_ids, (1, 2))


class HaierCoverageSqliteTests(unittest.TestCase):
    def test_audit_loads_sibling_xml_without_writing_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            database = root / "inspection.db"
            xml_path = root / "reports" / "inspection" / "7" / "home.xml"
            xml_path.parent.mkdir(parents=True)
            xml_path.write_text(
                '<hierarchy><node text="首页" content-desc="分类 许愿池 购物车 我的" /></hierarchy>',
                encoding="utf-8",
            )
            invalid_xml = root / "reports" / "inspection" / "7" / "invalid.xml"
            invalid_xml.write_text(
                '<hierarchy><node text="poison">',
                encoding="utf-8",
            )
            foreign_xml = root / "reports" / "inspection" / "8" / "foreign.xml"
            foreign_xml.parent.mkdir(parents=True)
            foreign_xml.write_text(
                '<hierarchy><node text="poison" /></hierarchy>',
                encoding="utf-8",
            )
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE inspectionrun (
                    id INTEGER PRIMARY KEY, name TEXT, package_name TEXT, status TEXT
                );
                CREATE TABLE inspectionstate (
                    id INTEGER PRIMARY KEY, run_id INTEGER, branch_run_id INTEGER,
                    page_subtype TEXT, semantic_key TEXT, instance_anchor TEXT,
                    activity TEXT, foreground_package TEXT, xml_path TEXT
                );
                CREATE TABLE inspectiontransition (
                    id INTEGER PRIMARY KEY, run_id INTEGER, branch_run_id INTEGER,
                    from_state_id INTEGER, to_state_id INTEGER, action_type TEXT,
                    action_key TEXT, action_role TEXT, execution_disposition TEXT,
                    status TEXT, failure_type TEXT, risk_type TEXT, reason TEXT
                );
                INSERT INTO inspectionrun VALUES (
                    7, 'fixture', 'com.ehaier.zgq.shop.mall', 'SUCCESS'
                );
                INSERT INTO inspectionstate VALUES (
                    1, 7, 1, 'HOME', '', '', '', 'com.ehaier.zgq.shop.mall',
                    'inspection/7/home.xml'
                );
                INSERT INTO inspectionstate VALUES (
                    2, 7, 1, 'UNKNOWN', '', '', '', 'com.ehaier.zgq.shop.mall',
                    'inspection/7/invalid.xml'
                );
                INSERT INTO inspectionstate VALUES (
                    3, 7, 1, 'UNKNOWN', '', '', '', 'com.ehaier.zgq.shop.mall',
                    'inspection/8/foreign.xml'
                );
                """
            )
            connection.commit()
            before = database.read_bytes()
            connection.close()
            manifest = (
                ManifestItem(
                    "home_xml",
                    "home xml",
                    1.0,
                    True,
                    "state",
                    StateMatcher(("HOME",), (r"许愿池",)),
                ),
            )

            report = audit_haier_coverage(database, 7, manifest=manifest)

            self.assertTrue(report.passed)
            self.assertEqual(report.xml_loaded_count, 1)
            self.assertEqual(report.xml_missing_count, 2)
            self.assertEqual(report.items[0].evidence_state_ids, (1,))
            poison_report = audit_haier_coverage(
                database,
                7,
                threshold=0,
                manifest=(
                    ManifestItem(
                        "poison",
                        "poison",
                        1.0,
                        True,
                        "state",
                        StateMatcher(xml_patterns=(r"poison",)),
                    ),
                ),
            )
            self.assertFalse(poison_report.items[0].covered)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((root / "inspection.db-wal").exists())
            self.assertFalse((root / "inspection.db-shm").exists())
            self.assertFalse((root / "inspection.db-journal").exists())
            with redirect_stdout(io.StringIO()):
                strict_exit = coverage_cli_main(
                    ["7", "--database", str(database), "--strict"]
                )
                advisory_exit = coverage_cli_main(["7", "--database", str(database)])
            self.assertEqual(strict_exit, 1)
            self.assertEqual(advisory_exit, 0)
            self.assertEqual(database.read_bytes(), before)

    def test_audit_refuses_wal_when_sqlite_would_create_shm(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source.db"
            target = root / "target.db"
            connection = sqlite3.connect(source)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE evidence (value TEXT)")
            connection.execute("INSERT INTO evidence VALUES ('in wal')")
            connection.commit()
            shutil.copy2(source, target)
            shutil.copy2(Path(f"{source}-wal"), Path(f"{target}-wal"))
            connection.close()

            with self.assertRaisesRegex(CoverageAuditError, "without an existing -shm"):
                audit_haier_coverage(target, 1)

            self.assertFalse(Path(f"{target}-shm").exists())

    def test_cli_returns_structured_error_for_out_of_range_run_id(self):
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = coverage_cli_main(
                [str(1 << 80), "--database", "/does/not/matter.db"]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("SQLite 64-bit", output.getvalue())


if __name__ == "__main__":
    unittest.main()
