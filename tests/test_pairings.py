import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import (
    ApiError,
    PharmacovigilanceService,
    iso,
    parse_time,
    report_deadline,
    utcnow,
)

REPORTER = ("reporter-a", "reporter", "CN")
LEAD = ("lead-cn", "regional_lead", "CN")
REVIEWER = ("reviewer-1", "medical_reviewer")


class PairingFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.db"
        self.svc = PharmacovigilanceService(self.db)
        self.t0 = utcnow() - timedelta(days=30)

    def tearDown(self):
        self.tmp.cleanup()

    def create_case(self, product="DrugA", event_term="肝损伤", serious=False, fatal=False,
                    patient="P-1", dedupe="intake-1", received=None):
        return self.svc.create_case(
            *REPORTER,
            {
                "patient_ref": patient, "region": "CN", "product": product,
                "event_term": event_term, "source": "email", "dedupe_key": dedupe,
                "received_at": iso(received or self.t0), "serious": serious, "fatal": fatal,
            },
        )["case"]

    def followup(self, case_id, expected, **kwargs):
        body = {"content": "随访更新", "source": "phone", "expected_revision": expected,
                "received_at": iso(kwargs.pop("received", utcnow()))}
        body.update(kwargs)
        return self.svc.add_followup(case_id, *REPORTER, body)

    # ----- 多搭配 / 整体最重 -----

    def test_followup_adds_pairings_and_case_takes_worst(self):
        case = self.create_case()
        self.assertEqual(case["pairing_count"], 1)
        self.assertEqual(case["serious"], 0)
        primary = case["pairings"][0]

        # 随访补两组新搭配：一组严重，一组非严重
        t1 = self.t0 + timedelta(days=5)
        result = self.followup(
            case["id"], 1, received=t1,
            new_pairings=[
                {"product": "DrugB", "event_term": "皮疹", "serious": False},
                {"product": "DrugC", "event_term": "心律失常", "serious": True},
            ],
        )
        self.assertEqual(len(result["created_pairings"]), 2)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        pairings = detail["case"]["pairings"]
        self.assertEqual([p["seq"] for p in pairings], [1, 2, 3])
        self.assertEqual(pairings[0]["id"], primary["id"])
        # 案例整体取最重：严重档
        self.assertEqual(detail["case"]["serious"], 1)
        self.assertEqual(detail["case"]["fatal"], 0)
        self.assertEqual(detail["case"]["severity_rank"], 1)
        self.assertEqual(detail["case"]["product"], "DrugA")  # 主搭配字段平铺

        # 同一请求里重复提交同一组搭配：只建一份，第二份幂等去重
        again = self.followup(
            case["id"], 2, received=t1 + timedelta(days=1),
            new_pairings=[{"product": " drugb ", "event_term": "皮疹"},
                          {"product": "DrugB", "event_term": "皮疹"}],
        )
        self.assertEqual(again["created_pairings"], [])
        self.assertEqual(len(again["deduplicated_pairings"]), 1)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["pairing_count"], 3)

        # 死亡必须先严重
        with self.assertRaises(ApiError) as ctx:
            self.followup(case["id"], 3, new_pairings=[{"product": "DrugX", "event_term": "猝死", "fatal": True}])
        self.assertEqual(ctx.exception.code, "invalid_severity")

    def test_case_overall_becomes_fatal_via_pairing_update(self):
        case = self.create_case()
        primary_id = case["primary_pairing_id"]
        self.followup(
            case["id"], 1, received=self.t0 + timedelta(days=3),
            pairing_updates=[{"pairing_id": primary_id, "serious": True, "fatal": True}],
        )
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["fatal"], 1)
        self.assertEqual(detail["case"]["severity_rank"], 2)
        pairing = detail["case"]["pairings"][0]
        self.assertTrue(pairing["fatal"])
        self.assertEqual(parse_time(pairing["anchor_at"]), parse_time(iso(self.t0 + timedelta(days=3))))

    # ----- 报告按搭配出具、同国同搭配唯一、期限重算 -----

    def test_reports_per_pairing_and_deadline_recompute(self):
        case = self.create_case()  # 非严重，期限 90 天
        primary_id = case["primary_pairing_id"]

        cn_pending = self.svc.create_report(case["id"], *LEAD, {"country": "cn"})
        self.assertEqual(parse_time(cn_pending["due_at"]),
                         parse_time(iso(self.t0 + timedelta(days=90))))
        us_report = self.svc.create_report(case["id"], *LEAD,
                                           {"country": "US", "pairing_id": primary_id})
        submitted = self.svc.submit_report(us_report["id"], *LEAD, {})
        self.assertEqual(submitted["report"]["status"], "submitted")
        original_us_due = submitted["report"]["due_at"]

        # 同一国家同一搭配只留一份
        with self.assertRaises(ApiError) as ctx:
            self.svc.create_report(case["id"], *LEAD, {"country": "CN"})
        self.assertEqual(ctx.exception.code, "report_exists")

        # 随访：主搭配转死亡 + 补一组新搭配
        t1 = self.t0 + timedelta(days=10)
        result = self.followup(
            case["id"], 1, received=t1,
            pairing_updates=[{"pairing_id": primary_id, "serious": True, "fatal": True}],
            new_pairings=[{"product": "DrugB", "event_term": "皮疹", "serious": False}],
        )
        self.assertEqual(result["reports_recomputed"], 1)  # 仅 CN 未交报告重算

        detail = self.svc.get_case(case["id"], "global_admin", "")
        reports = {r["country"]: r for r in detail["reports"]}
        # 未交：按新信息接收时间重算为死亡 7 天
        self.assertEqual(parse_time(reports["CN"]["due_at"]),
                         parse_time(iso(t1 + timedelta(days=7))))
        self.assertEqual(reports["CN"]["status"], "pending")
        # 已交：期限不动
        self.assertEqual(reports["US"]["due_at"], original_us_due)
        self.assertEqual(reports["US"]["status"], "submitted")

        # 后补搭配另出报告，与主搭配的 CN 报告并存
        secondary_id = next(p["id"] for p in detail["case"]["pairings"] if p["seq"] == 2)
        second_cn = self.svc.create_report(case["id"], *LEAD,
                                           {"country": "CN", "pairing_id": secondary_id})
        self.assertEqual(parse_time(second_cn["due_at"]),
                         parse_time(iso(t1 + timedelta(days=90))))
        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(len(detail["reports"]), 3)
        # 不能给不属于该案例的搭配出报告
        with self.assertRaises(ApiError) as ctx:
            other = self.create_case(patient="P-9", dedupe="intake-other")
            self.svc.create_report(case["id"], *LEAD,
                                   {"country": "JP", "pairing_id": other["primary_pairing_id"]})
        self.assertEqual(ctx.exception.code, "pairing_not_found")

    def test_medical_review_targets_pairing_and_recomputes_open_reports(self):
        case = self.create_case(serious=True)  # 严重 15 天
        pending = self.svc.create_report(case["id"], *LEAD, {"country": "CN"})
        review_time = self.t0 + timedelta(days=2)
        result = self.svc.medical_review(
            case["id"], *REVIEWER,
            {"expected_revision": 1, "serious": True, "fatal": True,
             "causality": "related", "rationale": "死亡证明核验",
             "received_at": iso(review_time)},
        )
        self.assertEqual(result["reports_recomputed"], 1)
        detail = self.svc.get_case(case["id"], "global_admin", "")
        cn_report = next(r for r in detail["reports"] if r["country"] == "CN")
        self.assertEqual(parse_time(cn_report["due_at"]),
                         parse_time(iso(review_time + timedelta(days=7))))
        # 转归不变的复审不再重算
        again = self.svc.medical_review(
            case["id"], *REVIEWER,
            {"expected_revision": 2, "serious": True, "fatal": True,
             "causality": "certain", "rationale": "补充尸检", "received_at": iso(utcnow())},
        )
        self.assertEqual(again["reports_recomputed"], 0)

    # ----- 并发：同一组搭配只建一份 -----

    def test_concurrent_same_pairing_creates_only_one(self):
        case = self.create_case()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def submit():
            barrier.wait()
            try:
                outcomes.append(self.svc.add_followup(
                    case["id"], *REPORTER,
                    {"content": "并发补搭配", "source": "phone", "expected_revision": 1,
                     "received_at": iso(utcnow()),
                     "new_pairings": [{"product": "DrugB", "event_term": "皮疹"}]},
                ))
            except ApiError as exc:
                outcomes.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        detail = self.svc.get_case(case["id"], "global_admin", "")
        self.assertEqual(detail["case"]["pairing_count"], 2)
        successes = [o for o in outcomes if not isinstance(o, ApiError)]
        conflicts = [o for o in outcomes if isinstance(o, ApiError) and o.code == "revision_conflict"]
        self.assertEqual(len(successes) + len(conflicts), 2)
        self.assertGreaterEqual(len(successes), 1)

        # 失败者重读后按当前版本重提：命中幂等去重，仍只一份
        if conflicts:
            current_rev = detail["case"]["revision"]
            retry = self.followup(case["id"], current_rev,
                                  new_pairings=[{"product": "DrugB", "event_term": "皮疹"}])
            self.assertEqual(retry["created_pairings"], [])
            self.assertEqual(len(retry["deduplicated_pairings"]), 1)
            detail = self.svc.get_case(case["id"], "global_admin", "")
            self.assertEqual(detail["case"]["pairing_count"], 2)

    # ----- 合并：同患者，搭配并集，同国同搭配报告去重（优先已提交） -----

    def test_merge_unions_pairings_and_dedupes_reports(self):
        case_a = self.create_case(product="DrugA", event_term="肝损伤", dedupe="a",
                                  patient="P-1", received=self.t0)
        case_b = self.create_case(product="DrugB", event_term="皮疹", dedupe="b",
                                  patient="P-1", received=self.t0 + timedelta(days=1))
        # B 的主搭配与 A 重复（同药同事件，大小写/空白归一化）
        self.followup(case_b["id"], 1, received=self.t0 + timedelta(days=2),
                      new_pairings=[{"product": "DrugA", "event_term": " 肝损伤 "}])

        report_a = self.svc.create_report(case_a["id"], *LEAD, {"country": "CN"})
        self.svc.submit_report(report_a["id"], *LEAD, {})  # A 的 CN 已提交
        self.svc.create_report(case_b["id"], *LEAD, {"country": "CN"})  # B 的 CN 未提交
        # 不同国家的不冲突
        self.svc.create_report(case_a["id"], *LEAD, {"country": "US"})
        self.svc.create_report(case_b["id"], *LEAD, {"country": "DE"})

        merged = self.svc.merge_cases(case_b["id"], "admin-1", "global_admin",
                                      {"target_case_id": case_a["id"]})
        self.assertFalse(merged["idempotent"])
        detail = self.svc.get_case(case_a["id"], "global_admin", "")
        products = {(p["product"], p["event_term"]) for p in detail["case"]["pairings"]}
        self.assertEqual(products, {("DrugA", "肝损伤"), ("DrugB", "皮疹")})
        reports = {(r["pairing_seq"], r["country"], r["status"]) for r in detail["reports"]}
        # DrugA/CN 只保留一份且是已提交那份；DrugA 有 US；DrugB 有 DE
        drug_a_cn = [r for r in reports if r[0] == 1 and r[1] == "CN"]
        self.assertEqual(len(drug_a_cn), 1)
        self.assertEqual(drug_a_cn[0][2], "submitted")
        self.assertIn((1, "US", "pending"), reports)
        self.assertIn((2, "DE", "pending"), reports)
        self.assertEqual(len(detail["intakes"]), 2)

        # 患者不同不能合并
        other = self.create_case(product="DrugA", event_term="肝损伤", dedupe="c", patient="P-2")
        with self.assertRaises(ApiError) as ctx:
            self.svc.merge_cases(other["id"], "admin-1", "global_admin",
                                 {"target_case_id": case_a["id"]})
        self.assertEqual(ctx.exception.code, "merge_conflict")

    # ----- 旧数据升级 -----

    def test_legacy_schema_migrates_into_primary_pairing(self):
        self.svc.repo.conn.close()
        self.db.unlink()
        for suffix in ("-wal", "-shm"):
            p = Path(str(self.db) + suffix)
            if p.exists():
                p.unlink()
        legacy = sqlite3.connect(self.db)
        legacy.executescript(
            """
            CREATE TABLE cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_no TEXT NOT NULL UNIQUE,
                patient_ref TEXT NOT NULL, region TEXT NOT NULL, product TEXT NOT NULL,
                event_term TEXT NOT NULL, onset_at TEXT, received_at TEXT NOT NULL,
                serious INTEGER NOT NULL DEFAULT 0, fatal INTEGER NOT NULL DEFAULT 0,
                causality TEXT, report_due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                revision INTEGER NOT NULL DEFAULT 1, merged_into INTEGER REFERENCES cases(id),
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE intakes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER REFERENCES cases(id),
                source TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                content TEXT NOT NULL, source TEXT NOT NULL, received_at TEXT NOT NULL,
                revision INTEGER NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(case_id, revision)
            );
            CREATE TABLE reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                country TEXT NOT NULL, due_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT, submitted_by TEXT, late INTEGER NOT NULL DEFAULT 0,
                UNIQUE(case_id, country)
            );
            CREATE TABLE medical_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER NOT NULL REFERENCES cases(id),
                case_revision INTEGER NOT NULL, serious INTEGER NOT NULL, fatal INTEGER NOT NULL,
                causality TEXT NOT NULL, rationale TEXT NOT NULL, reviewer TEXT NOT NULL,
                created_at TEXT NOT NULL, UNIQUE(case_id, case_revision)
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, actor TEXT NOT NULL,
                role TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        old_due = iso(report_deadline(self.t0, True, False))
        now = iso()
        legacy.execute(
            """INSERT INTO cases(case_no,patient_ref,region,product,event_term,received_at,serious,fatal,
               causality,report_due_at,status,revision,created_by,created_at,updated_at)
               VALUES('PV-OLD-1','P-1','CN','OldDrug','旧事件',?,1,0,'possibly',?,'open',1,?,?,?)""",
            (iso(self.t0), old_due, now, now, now),
        )
        legacy.execute(
            """INSERT INTO reports(case_id,country,due_at,status,submitted_at,submitted_by,late)
               VALUES(1,'CN',?,'submitted',?,'lead-cn',0)""",
            (old_due, iso(self.t0 + timedelta(days=10))),
        )
        legacy.execute(
            """INSERT INTO medical_reviews(case_id,case_revision,serious,fatal,causality,rationale,
               reviewer,created_at) VALUES(1,1,1,0,'possibly','旧裁定','reviewer-1',?)""",
            (now,),
        )
        legacy.commit()
        legacy.close()

        # 打开旧库即触发升级
        svc = PharmacovigilanceService(self.db)
        detail = svc.get_case(1, "global_admin", "")
        case = detail["case"]
        self.assertEqual(case["case_no"], "PV-OLD-1")
        self.assertEqual(case["pairing_count"], 1)
        primary = case["pairings"][0]
        self.assertEqual(primary["seq"], 1)
        self.assertEqual(primary["product"], "OldDrug")
        self.assertEqual(primary["event_term"], "旧事件")
        self.assertTrue(primary["serious"])
        # 旧报告继续挂在第一组搭配上，且保持已受理状态、期限不变
        self.assertEqual(len(detail["reports"]), 1)
        old_report = detail["reports"][0]
        self.assertEqual(old_report["pairing_id"], primary["id"])
        self.assertEqual(old_report["status"], "submitted")
        self.assertEqual(old_report["due_at"], old_due)
        # 旧审核记录也挂在第一组搭配上
        self.assertEqual(detail["reviews"][0]["pairing_id"], primary["id"])

        # 升级后可正常补新搭配并另出报告
        result = svc.add_followup(
            1, *REPORTER,
            {"content": "升级后随访", "source": "phone", "expected_revision": 1,
             "received_at": iso(self.t0 + timedelta(days=20)),
             "new_pairings": [{"product": "NewDrug", "event_term": "新事件", "serious": True}]},
        )
        self.assertEqual(len(result["created_pairings"]), 1)
        new_id = result["created_pairings"][0]["id"]
        new_report = svc.create_report(1, *LEAD, {"country": "CN", "pairing_id": new_id})
        self.assertEqual(new_report["pairing_id"], new_id)
        # 再次打开不重复迁移
        PharmacovigilanceService(self.db).repo.conn.close()
        svc.repo.conn.close()


if __name__ == "__main__":
    unittest.main()
