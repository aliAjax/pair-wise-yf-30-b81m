import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, parse_time, report_deadline

FIXED = iso(datetime(2026, 1, 1, tzinfo=timezone.utc))


class ComboFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, dedupe="intake-1", product="DrugA", event="肝损伤", serious=False, fatal=False):
        return self.svc.create_case(
            "reporter-a", "reporter", "CN",
            {"patient_ref": "P-1", "region": "CN", "product": product, "event_term": event,
             "source": "email", "dedupe_key": dedupe, "received_at": FIXED,
             "serious": serious, "fatal": fatal},
        )["case"]

    def review(self, case_id, expected, targets, received=FIXED):
        body = {"expected_revision": expected, "rationale": "医学裁定", "received_at": received}
        if isinstance(targets, dict):
            body.update(targets)
        else:
            body["combos"] = targets
        return self.svc.medical_review(case_id, "reviewer-1", "medical_reviewer", body)

    def test_primary_combo_on_intake(self):
        case = self.create()
        detail = self.svc.get_case(case["id"], "global_admin", "")
        combos = detail["combos"]
        self.assertEqual(len(combos), 1)
        primary = combos[0]
        self.assertEqual(primary["combo_no"], 1)
        self.assertEqual(primary["product"], "DrugA")
        self.assertEqual(primary["event_term"], "肝损伤")
        self.assertEqual(primary["case_id"], case["id"])
        self.assertIsNone(primary["followup_id"])
        self.assertEqual(detail["case"]["product"], "DrugA")
        self.assertEqual(detail["case"]["event_term"], "肝损伤")

    def test_followup_adds_new_combos(self):
        case = self.create()
        followed = self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "随访新增合并用药", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [
                 {"product": "DrugB", "event_term": "皮疹"},
                 {"product": "DrugC", "event_term": "发热"},
             ]},
        )
        self.assertEqual(followed["revision"], 2)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        combos = detail["combos"]
        self.assertEqual([c["combo_no"] for c in combos], [1, 2, 3])
        self.assertEqual(combos[1]["product"], "DrugB")
        self.assertEqual(combos[1]["event_term"], "皮疹")
        self.assertEqual(combos[2]["product"], "DrugC")
        self.assertEqual(combos[1]["followup_id"], detail["followups"][0]["id"])
        # 新搭配尚未裁定严重性
        self.assertEqual(combos[1]["serious"], 0)
        self.assertEqual(combos[2]["serious"], 0)

    def test_each_combo_keeps_its_own_outcome(self):
        case = self.create()
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "补报", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]},
        )
        self.review(case["id"], 2, [
            {"combo_id": 1, "serious": True, "fatal": False, "causality": "related"},
            {"combo_id": 2, "serious": True, "fatal": True, "causality": "possibly_related"},
        ])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        by_id = {c["id"]: c for c in detail["combos"]}
        self.assertEqual(by_id[1]["serious"], 1)
        self.assertEqual(by_id[1]["fatal"], 0)
        self.assertEqual(by_id[2]["serious"], 1)
        self.assertEqual(by_id[2]["fatal"], 1)
        # 案例整体取最重一档
        self.assertEqual(detail["case"]["serious"], 1)
        self.assertEqual(detail["case"]["fatal"], 1)
        self.assertEqual(detail["case"]["revision"], 3)

    def test_case_aggregate_takes_worst_tier(self):
        # 一例严重、一例非严重 -> 整体严重但非死亡
        case = self.create()
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "补报", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]},
        )
        self.review(case["id"], 2, [
            {"combo_id": 1, "serious": True, "fatal": False, "causality": "related"},
            {"combo_id": 2, "serious": False, "fatal": False, "causality": "unrelated"},
        ])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["serious"], 1)
        self.assertEqual(detail["case"]["fatal"], 0)
        # 整体期限按严重档（15 天）
        due = parse_time(detail["case"]["report_due_at"])
        self.assertEqual(due, parse_time(FIXED) + timedelta(days=15))

    def test_review_without_combo_targets_primary(self):
        case = self.create()
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "补报", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]},
        )
        self.review(case["id"], 2, {"serious": True, "fatal": True, "causality": "related"})
        detail = self.svc.get_case(case["id"], "global_admin", "")
        by_id = {c["id"]: c for c in detail["combos"]}
        self.assertEqual(by_id[1]["fatal"], 1)
        self.assertEqual(by_id[2]["serious"], 0)
        self.assertEqual(detail["case"]["fatal"], 1)

    def test_reports_are_per_combo_per_country_and_idempotent(self):
        case = self.create()
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "补报", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]},
        )
        r1 = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        r2 = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.assertEqual(r1["id"], r2["id"], "同一国家同一搭配只留一份")
        r_us = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "US"})
        self.assertNotEqual(r1["id"], r_us["id"])
        combo2 = self.svc.get_case(case["id"], "global_admin", "")["combos"][1]
        r_c2 = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN",
                                      {"country": "CN", "combo_id": combo2["id"]})
        self.assertNotEqual(r1["id"], r_c2["id"])
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["reports"]), 3)
        # 直接查库确认 (combo_id, country) 唯一
        rows = self.svc.repo.conn.execute(
            "SELECT combo_id,country,COUNT(*) c FROM reports GROUP BY combo_id,country HAVING c>1"
        ).fetchall()
        self.assertEqual(rows, [])

    def test_concurrent_submission_same_combo_creates_one_report(self):
        case = self.create()
        errors = []
        results = []

        def submit(actor):
            try:
                results.append(self.svc.create_report(
                    case["id"], actor, "regional_lead", "CN", {"country": "CN"}))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=submit, args=(f"lead-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        ids = {r["id"] for r in results}
        self.assertEqual(len(ids), 1, "并发提交同一搭配只建一份")
        count = self.svc.repo.conn.execute(
            "SELECT COUNT(*) FROM reports WHERE case_id=? AND country='CN'", (case["id"],)
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_outcome_change_recalculates_pending_deadline(self):
        case = self.create()
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN", {"country": "CN"})
        # 初始非严重 -> 90 天
        self.assertEqual(parse_time(report["due_at"]), parse_time(FIXED) + timedelta(days=90))
        self.review(case["id"], 1, {"serious": True, "fatal": True, "causality": "related"})
        updated = self.svc.repo.conn.execute("SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
        self.assertEqual(parse_time(updated["due_at"]), parse_time(FIXED) + timedelta(days=7))
        # 已提交的报告期限不再重算
        self.svc.submit_report(report["id"], "lead-cn", "regional_lead", "CN", {})
        self.review(case["id"], 2, {"serious": False, "fatal": False, "causality": "unrelated"})
        after = self.svc.repo.conn.execute("SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
        self.assertEqual(after["status"], "submitted")
        self.assertEqual(parse_time(after["due_at"]), parse_time(FIXED) + timedelta(days=7))

    def test_pending_report_of_other_combo_recalculated_on_review(self):
        case = self.create()
        self.svc.add_followup(
            case["id"], "reporter-a", "reporter", "CN",
            {"content": "补报", "source": "phone", "expected_revision": 1,
             "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]},
        )
        combo2 = self.svc.get_case(case["id"], "global_admin", "")["combos"][1]
        report = self.svc.create_report(case["id"], "lead-cn", "regional_lead", "CN",
                                       {"country": "CN", "combo_id": combo2["id"]})
        self.assertEqual(parse_time(report["due_at"]), parse_time(FIXED) + timedelta(days=90))
        self.review(case["id"], 2, [
            {"combo_id": combo2["id"], "serious": True, "fatal": False, "causality": "related"},
        ])
        updated = self.svc.repo.conn.execute("SELECT * FROM reports WHERE id=?", (report["id"],)).fetchone()
        self.assertEqual(parse_time(updated["due_at"]), parse_time(FIXED) + timedelta(days=15))

    def test_migration_legacy_data(self):
        import sqlite3
        legacy = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(legacy)
        conn.executescript(
            """
            CREATE TABLE cases (id INTEGER PRIMARY KEY AUTOINCREMENT, case_no TEXT NOT NULL UNIQUE,
                patient_ref TEXT NOT NULL, region TEXT NOT NULL, product TEXT NOT NULL, event_term TEXT NOT NULL,
                onset_at TEXT, received_at TEXT NOT NULL, serious INTEGER NOT NULL DEFAULT 0,
                fatal INTEGER NOT NULL DEFAULT 0, causality TEXT, report_due_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
                merged_into INTEGER, created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE intakes (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, source TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL, received_at TEXT NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE followups (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL,
                content TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL, revision INTEGER NOT NULL,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(case_id, revision));
            CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL,
                country TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT, submitted_by TEXT, late INTEGER NOT NULL DEFAULT 0, UNIQUE(case_id, country));
            CREATE TABLE medical_reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL,
                case_revision INTEGER NOT NULL, serious INTEGER NOT NULL, fatal INTEGER NOT NULL,
                causality TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(case_id, case_revision));
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, actor TEXT NOT NULL,
                role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL);
            """
        )
        conn.execute(
            """INSERT INTO cases(case_no,patient_ref,region,product,event_term,received_at,serious,fatal,
               causality,report_due_at,status,revision,created_by,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("PV-2026-000001", "P-1", "CN", "DrugA", "肝损伤", FIXED, 1, 1, "related",
             iso(parse_time(FIXED) + timedelta(days=7)), "open", 2,
             "reporter-a", FIXED, FIXED),
        )
        conn.execute(
            "INSERT INTO reports(case_id,country,due_at,status,submitted_at,submitted_by,late) VALUES(?,?,?,?,?,?,?)",
            (1, "CN", iso(parse_time(FIXED) + timedelta(days=7)), "submitted", FIXED, "lead-cn", 0),
        )
        conn.execute(
            """INSERT INTO medical_reviews(case_id,case_revision,serious,fatal,causality,rationale,reviewer,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (1, 1, 1, 1, "related", "旧裁定", "reviewer-1", FIXED),
        )
        conn.commit()
        conn.close()

        svc = PharmacovigilanceService(legacy)
        detail = svc.get_case(1, "global_admin", "")
        combos = detail["combos"]
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0]["combo_no"], 1)
        self.assertEqual(combos[0]["product"], "DrugA")
        self.assertEqual(combos[0]["event_term"], "肝损伤")
        self.assertEqual(combos[0]["serious"], 1)
        self.assertEqual(combos[0]["fatal"], 1)
        # 旧报告挂在第一组搭配上
        self.assertEqual(len(detail["reports"]), 1)
        self.assertEqual(detail["reports"][0]["combo_id"], combos[0]["id"])
        self.assertEqual(detail["reports"][0]["status"], "submitted")
        # 旧医学裁定也挂到第一组
        self.assertEqual(detail["reviews"][0]["combo_id"], combos[0]["id"])
        # 同一国家同一搭配再出报告 -> 只留一份
        again = svc.create_report(1, "lead-cn", "regional_lead", "CN", {"country": "CN"})
        self.assertEqual(again["id"], detail["reports"][0]["id"])
        # 后补搭配另出报告
        svc.add_followup(1, "reporter-a", "reporter", "CN",
                         {"content": "补报", "source": "phone", "expected_revision": 2,
                          "received_at": FIXED, "combos": [{"product": "DrugB", "event_term": "皮疹"}]})
        combo2 = svc.get_case(1, "global_admin", "")["combos"][1]
        new_report = svc.create_report(1, "lead-cn", "regional_lead", "CN",
                                       {"country": "CN", "combo_id": combo2["id"]})
        self.assertNotEqual(new_report["id"], detail["reports"][0]["id"])
        all_reports = svc.get_case(1, "global_admin", "")["reports"]
        self.assertEqual(len(all_reports), 2)


if __name__ == "__main__":
    unittest.main()
