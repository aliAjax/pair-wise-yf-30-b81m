#!/usr/bin/env python3
"""Pharmacovigilance case intake service using only the Python standard library.

一条案例可挂多组「可疑药品 + 事件词」搭配（pairings）：
- 首报建立一组主搭配（seq=1），随访可补新搭配或更新既有搭配的转归；
- 严重性/死亡/关联性按搭配记录，案例整体取最重一档（死亡 > 严重 > 非严重）；
- 分国家报告按搭配出具，同一国家同一搭配只留一份；
- 搭配转归变化时，该搭配所有未提交报告的期限按新信息接收时间重算；
- 旧库升级时，原药品/事件词归入第一组搭配，旧报告继续挂在它上面。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = 8201
ROLES = {"reporter", "regional_lead", "medical_reviewer", "global_admin"}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    current = value or utcnow()
    return current.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None, default: datetime | None = None) -> datetime:
    if not value:
        if default is None:
            raise ApiError(400, "missing_time", "必须提供 ISO 8601 时间")
        return default
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def norm_key(value: str) -> str:
    """搭配去重用的归一化键：去首尾空白、折叠空白、忽略大小写。"""
    return " ".join(value.strip().casefold().split())


def report_deadline(received_at: datetime, serious: bool, fatal: bool) -> datetime:
    if serious:
        return received_at + timedelta(days=7 if fatal else 15)
    return received_at + timedelta(days=90)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_no TEXT NOT NULL UNIQUE,
    patient_ref TEXT NOT NULL,
    region TEXT NOT NULL,
    onset_at TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    revision INTEGER NOT NULL DEFAULT 1,
    merged_into INTEGER REFERENCES cases(id),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pairings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    seq INTEGER NOT NULL,
    product TEXT NOT NULL,
    event_term TEXT NOT NULL,
    product_key TEXT NOT NULL,
    event_key TEXT NOT NULL,
    serious INTEGER NOT NULL DEFAULT 0,
    fatal INTEGER NOT NULL DEFAULT 0,
    causality TEXT,
    anchor_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(case_id, seq),
    UNIQUE(case_id, product_key, event_key)
);
CREATE TABLE IF NOT EXISTS intakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER REFERENCES cases(id),
    source TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    received_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    content TEXT NOT NULL,
    source TEXT NOT NULL,
    received_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(case_id, revision)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    pairing_id INTEGER NOT NULL REFERENCES pairings(id),
    country TEXT NOT NULL,
    due_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    submitted_at TEXT,
    submitted_by TEXT,
    late INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pairing_id, country)
);
CREATE TABLE IF NOT EXISTS medical_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    pairing_id INTEGER NOT NULL REFERENCES pairings(id),
    case_revision INTEGER NOT NULL,
    serious INTEGER NOT NULL,
    fatal INTEGER NOT NULL,
    causality TEXT NOT NULL,
    rationale TEXT NOT NULL,
    reviewer TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(pairing_id, case_revision)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id INTEGER,
    actor TEXT NOT NULL,
    role TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._write_lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()
        self.conn.execute("PRAGMA foreign_keys=ON")

    @contextmanager
    def tx(self):
        # 进程内写事务串行化；唯一索引再兜底跨进程/跨实例的并发提交
        with self._write_lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def _table_columns(self, table: str) -> set[str] | None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            return None
        return {row[1] for row in rows}

    def init_schema(self) -> None:
        columns = self._table_columns("cases")
        if columns is None or "product" not in columns:
            # 全新库，或已是新结构（CREATE IF NOT EXISTS 幂等补建缺失表）
            self.conn.executescript(SCHEMA_SQL)
            return
        self.migrate_legacy()

    def migrate_legacy(self) -> None:
        """旧库升级：原药品/事件词归入第一组搭配，旧报告与旧审核挂到该搭配。"""
        conn = self.conn
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(
                """
                CREATE TABLE cases_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_no TEXT NOT NULL UNIQUE,
                    patient_ref TEXT NOT NULL,
                    region TEXT NOT NULL,
                    onset_at TEXT,
                    received_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1,
                    merged_into INTEGER REFERENCES cases_new(id),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE pairings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases_new(id),
                    seq INTEGER NOT NULL,
                    product TEXT NOT NULL,
                    event_term TEXT NOT NULL,
                    product_key TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    serious INTEGER NOT NULL DEFAULT 0,
                    fatal INTEGER NOT NULL DEFAULT 0,
                    causality TEXT,
                    anchor_at TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(case_id, seq),
                    UNIQUE(case_id, product_key, event_key)
                );
                CREATE TABLE reports_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases_new(id),
                    pairing_id INTEGER NOT NULL REFERENCES pairings(id),
                    country TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    submitted_at TEXT,
                    submitted_by TEXT,
                    late INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(pairing_id, country)
                );
                CREATE TABLE medical_reviews_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id INTEGER NOT NULL REFERENCES cases_new(id),
                    pairing_id INTEGER NOT NULL REFERENCES pairings(id),
                    case_revision INTEGER NOT NULL,
                    serious INTEGER NOT NULL,
                    fatal INTEGER NOT NULL,
                    causality TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    reviewer TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(pairing_id, case_revision)
                );
                """
            )
            conn.execute(
                """INSERT INTO cases_new(id,case_no,patient_ref,region,onset_at,received_at,status,revision,
                   merged_into,created_by,created_at,updated_at)
                   SELECT id,case_no,patient_ref,region,onset_at,received_at,status,revision,
                   merged_into,created_by,created_at,updated_at FROM cases"""
            )
            pairing_for_case: dict[int, int] = {}
            for old_case in conn.execute("SELECT * FROM cases"):
                cursor = conn.execute(
                    """INSERT INTO pairings(case_id,seq,product,event_term,product_key,event_key,serious,fatal,
                       causality,anchor_at,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        old_case["id"], 1, old_case["product"], old_case["event_term"],
                        norm_key(old_case["product"]), norm_key(old_case["event_term"]),
                        old_case["serious"], old_case["fatal"], old_case["causality"],
                        old_case["received_at"], old_case["created_by"],
                        old_case["created_at"], old_case["updated_at"],
                    ),
                )
                pairing_for_case[old_case["id"]] = cursor.lastrowid
            for old_report in conn.execute("SELECT * FROM reports"):
                conn.execute(
                    """INSERT INTO reports_new(id,case_id,pairing_id,country,due_at,status,submitted_at,
                       submitted_by,late) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (
                        old_report["id"], old_report["case_id"],
                        pairing_for_case[old_report["case_id"]], old_report["country"],
                        old_report["due_at"], old_report["status"], old_report["submitted_at"],
                        old_report["submitted_by"], old_report["late"],
                    ),
                )
            for old_review in conn.execute("SELECT * FROM medical_reviews"):
                conn.execute(
                    """INSERT INTO medical_reviews_new(id,case_id,pairing_id,case_revision,serious,fatal,
                       causality,rationale,reviewer,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        old_review["id"], old_review["case_id"],
                        pairing_for_case[old_review["case_id"]], old_review["case_revision"],
                        old_review["serious"], old_review["fatal"], old_review["causality"],
                        old_review["rationale"], old_review["reviewer"], old_review["created_at"],
                    ),
                )
            conn.execute("DROP TABLE reports")
            conn.execute("DROP TABLE medical_reviews")
            conn.execute("DROP TABLE cases")
            conn.execute("ALTER TABLE cases_new RENAME TO cases")
            conn.execute("ALTER TABLE reports_new RENAME TO reports")
            conn.execute("ALTER TABLE medical_reviews_new RENAME TO medical_reviews")
            for table in ("cases", "pairings", "reports", "medical_reviews", "intakes", "followups"):
                max_id = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()[0]
                conn.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                if max_id:
                    conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, max_id))
        finally:
            conn.execute("PRAGMA foreign_keys=ON")

    @staticmethod
    def audit(conn: sqlite3.Connection, case_id: int | None, actor: str, role: str, action: str, detail: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO audit_log(case_id,actor,role,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
            (case_id, actor, role, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), iso()),
        )


class PharmacovigilanceService:
    def __init__(self, db_path: str | Path):
        self.repo = Repository(db_path)

    # ----- 身份与权限 -----

    @staticmethod
    def identity(headers: Any) -> tuple[str, str, str]:
        actor = headers.get("X-User-Id", "").strip()
        role = headers.get("X-Role", "").strip()
        region = headers.get("X-Region", "").strip()
        if not actor or role not in ROLES:
            raise ApiError(401, "unauthorized", "需要 X-User-Id 和有效的 X-Role")
        if role in {"reporter", "regional_lead"} and not region:
            raise ApiError(401, "region_required", "该角色必须提供 X-Region")
        return actor, role, region

    @staticmethod
    def can_access(case: dict[str, Any] | sqlite3.Row, role: str, region: str) -> bool:
        return role in {"medical_reviewer", "global_admin"} or case["region"] == region

    # ----- 基础读取 -----

    def _case(self, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not row:
            raise ApiError(404, "case_not_found", "案例不存在")
        return row

    @staticmethod
    def _pairings(conn: sqlite3.Connection, case_id: int) -> list[sqlite3.Row]:
        return list(conn.execute("SELECT * FROM pairings WHERE case_id=? ORDER BY seq", (case_id,)))

    @staticmethod
    def _pairing(conn: sqlite3.Connection, pairing_id: int, case_id: int | None = None) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM pairings WHERE id=?", (pairing_id,)).fetchone()
        if not row or (case_id is not None and row["case_id"] != case_id):
            raise ApiError(404, "pairing_not_found", "药品-事件搭配不存在")
        return row

    @classmethod
    def _primary_pairing(cls, conn: sqlite3.Connection, case_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM pairings WHERE case_id=? ORDER BY seq LIMIT 1", (case_id,)).fetchone()
        if not row:
            raise ApiError(409, "pairing_required", "该案例没有任何药品-事件搭配")
        return row

    @staticmethod
    def _resolve_pairing_ref(conn: sqlite3.Connection, case_id: int, ref: Any) -> sqlite3.Row:
        if isinstance(ref, int):
            return PharmacovigilanceService._pairing(conn, ref, case_id)
        if isinstance(ref, dict) and isinstance(ref.get("pairing_id"), int):
            return PharmacovigilanceService._pairing(conn, ref["pairing_id"], case_id)
        if isinstance(ref, dict) and isinstance(ref.get("seq"), int):
            row = conn.execute(
                "SELECT * FROM pairings WHERE case_id=? AND seq=?", (case_id, ref["seq"])
            ).fetchone()
            if not row:
                raise ApiError(404, "pairing_not_found", "药品-事件搭配不存在")
            return row
        raise ApiError(400, "pairing_required", "必须通过 pairing_id（或 seq）指定搭配")

    @classmethod
    def case_view(cls, conn: sqlite3.Connection, case_row: sqlite3.Row) -> dict[str, Any]:
        """案例视图：搭配数组 + 整体最重装归（死亡 > 严重 > 非严重），主搭配字段平铺便于旧客户端。"""
        pairings = [dict(row) for row in cls._pairings(conn, case_row["id"])]
        data = dict(case_row)
        serious = int(any(pairing["serious"] for pairing in pairings))
        fatal = int(any(pairing["fatal"] for pairing in pairings))
        primary = next((pairing for pairing in pairings if pairing["seq"] == 1), pairings[0] if pairings else None)
        data["serious"] = serious
        data["fatal"] = fatal
        data["severity_rank"] = 2 if fatal else 1 if serious else 0
        data["product"] = primary["product"] if primary else None
        data["event_term"] = primary["event_term"] if primary else None
        data["primary_pairing_id"] = primary["id"] if primary else None
        data["pairing_count"] = len(pairings)
        data["pairings"] = pairings
        return data

    @staticmethod
    def _recompute_open_reports(conn: sqlite3.Connection, pairing: sqlite3.Row, anchor: datetime) -> int:
        """转归变化后重算该搭配所有未交报告的期限；已提交报告不动。返回受影响份数。"""
        due = report_deadline(anchor, bool(pairing["serious"]), bool(pairing["fatal"]))
        cursor = conn.execute(
            "UPDATE reports SET due_at=? WHERE pairing_id=? AND status!='submitted'",
            (iso(due), pairing["id"]),
        )
        return cursor.rowcount

    @staticmethod
    def _bool_field(body: dict[str, Any], key: str, default: bool | None = None) -> bool | None:
        value = body.get(key, default)
        if value is None:
            return None if default is None else bool(default)
        if not isinstance(value, bool):
            raise ApiError(400, "invalid_field", f"{key} 必须是布尔值")
        return value

    # ----- 案例录入 -----

    def create_case(self, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        required = ("patient_ref", "region", "product", "event_term", "source", "dedupe_key")
        missing = [key for key in required if not str(body.get(key, "")).strip()]
        if missing:
            raise ApiError(400, "missing_fields", f"缺少字段: {', '.join(missing)}")
        if role == "reporter" and body["region"] != region:
            raise ApiError(403, "region_forbidden", "只能录入本区域案例")
        if role == "medical_reviewer" and body["region"] not in {"", region}:
            raise ApiError(403, "reviewer_region_forbidden", "医学审核员不能代表区域录入案例")
        product = body["product"].strip()
        event_term = body["event_term"].strip()
        serious = self._bool_field(body, "serious", False)
        fatal = self._bool_field(body, "fatal", False)
        if fatal and not serious:
            raise ApiError(400, "invalid_severity", "死亡搭配必须标记为严重")
        received = parse_time(body.get("received_at"), utcnow())
        now, received_iso = iso(), iso(received)
        with self.repo.tx() as conn:
            duplicate = conn.execute("SELECT * FROM intakes WHERE dedupe_key=?", (body["dedupe_key"],)).fetchone()
            if duplicate:
                case = self._case(conn, duplicate["case_id"])
                Repository.audit(conn, case["id"], actor, role, "intake_deduplicated",
                                 {"dedupe_key": body["dedupe_key"], "source": body["source"]})
                return {"deduplicated": True, "case": self.case_view(conn, case), "intake_id": duplicate["id"]}
            count = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] + 1
            case_no = body.get("case_no") or f"PV-{received.year}-{count:06d}"
            try:
                cursor = conn.execute(
                    """INSERT INTO cases(case_no,patient_ref,region,onset_at,received_at,
                       status,revision,created_by,created_at,updated_at)
                       VALUES(?,?,?,?,?,'open',1,?,?,?)""",
                    (case_no, body["patient_ref"], body["region"], body.get("onset_at"),
                     received_iso, actor, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "case_number_conflict", "案例编号已存在") from exc
            case_id = cursor.lastrowid
            conn.execute(
                """INSERT INTO pairings(case_id,seq,product,event_term,product_key,event_key,serious,fatal,
                   causality,anchor_at,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    case_id, 1, product, event_term, norm_key(product), norm_key(event_term),
                    int(serious), int(fatal), body.get("causality"), received_iso, actor, now, now,
                ),
            )
            conn.execute(
                "INSERT INTO intakes(case_id,source,dedupe_key,payload_json,received_at,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, body["source"], body["dedupe_key"],
                 json.dumps(body, ensure_ascii=False, sort_keys=True), received_iso, actor, now),
            )
            Repository.audit(conn, case_id, actor, role, "case_created",
                             {"case_no": case_no, "source": body["source"],
                              "pairing": {"product": product, "event_term": event_term,
                                          "serious": serious, "fatal": fatal}})
            return {"deduplicated": False, "case": self.case_view(conn, self._case(conn, case_id))}

    # ----- 查询 -----

    def get_case(self, case_id: int, role: str, region: str) -> dict[str, Any]:
        conn = self.repo.conn
        case = self._case(conn, case_id)
        if not self.can_access(case, role, region):
            raise ApiError(403, "case_forbidden", "无权查看该区域案例")
        return {
            "case": self.case_view(conn, case),
            "intakes": [dict(r) for r in conn.execute(
                "SELECT id,source,dedupe_key,received_at,created_by,created_at FROM intakes WHERE case_id=? ORDER BY id",
                (case_id,))],
            "followups": [dict(r) for r in conn.execute(
                "SELECT * FROM followups WHERE case_id=? ORDER BY revision", (case_id,))],
            "reports": [dict(r) for r in conn.execute(
                """SELECT r.*,p.seq AS pairing_seq,p.product AS pairing_product,
                          p.event_term AS pairing_event_term
                   FROM reports r JOIN pairings p ON p.id=r.pairing_id
                   WHERE r.case_id=? ORDER BY p.seq,r.country""", (case_id,))],
            "reviews": [dict(r) for r in conn.execute(
                "SELECT * FROM medical_reviews WHERE case_id=? ORDER BY id", (case_id,))],
            "audit": [dict(r) for r in conn.execute(
                "SELECT actor,role,action,detail_json,created_at FROM audit_log WHERE case_id=? ORDER BY id",
                (case_id,))] if role in {"medical_reviewer", "global_admin"} else [],
        }

    def list_cases(self, role: str, region: str, query: dict[str, list[str]]) -> list[dict[str, Any]]:
        sql = "SELECT * FROM cases WHERE status!='merged'"
        args: list[Any] = []
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND region=?"
            args.append(region)
        if query.get("status"):
            sql += " AND status=?"
            args.append(query["status"][0])
        sql += " ORDER BY received_at DESC,id DESC"
        conn = self.repo.conn
        return [self.case_view(conn, row) for row in conn.execute(sql, args)]

    # ----- 随访：补搭配 / 改转归 -----

    def add_followup(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        content = str(body.get("content", "")).strip()
        source = str(body.get("source", "")).strip()
        if not content or not source:
            raise ApiError(400, "missing_fields", "content 和 source 必填")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        new_items = body.get("new_pairings", [])
        update_items = body.get("pairing_updates", [])
        if not isinstance(new_items, list) or not isinstance(update_items, list):
            raise ApiError(400, "invalid_pairings", "new_pairings 和 pairing_updates 必须是数组")
        received = parse_time(body.get("received_at"), utcnow())
        now, received_iso = iso(), iso(received)
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region) or role in {"medical_reviewer"}:
                raise ApiError(403, "followup_forbidden", "当前角色不能提交随访")
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能再更新")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例已被其他人员更新，请重新读取")
            revision = expected + 1
            conn.execute(
                "INSERT INTO followups(case_id,content,source,received_at,revision,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (case_id, content, source, received_iso, revision, actor, now),
            )

            created: list[dict[str, Any]] = []
            deduplicated: list[dict[str, Any]] = []
            changed_pairing_ids: set[int] = set()

            # 更新既有搭配的严重/死亡转归
            for item in update_items:
                if not isinstance(item, dict):
                    raise ApiError(400, "invalid_pairing_update", "pairing_updates 元素必须是对象")
                pairing = self._resolve_pairing_ref(conn, case_id, item)
                new_serious = pairing["serious"]
                new_fatal = pairing["fatal"]
                if "serious" in item:
                    if not isinstance(item["serious"], bool):
                        raise ApiError(400, "invalid_field", "serious 必须是布尔值")
                    new_serious = int(item["serious"])
                if "fatal" in item:
                    if not isinstance(item["fatal"], bool):
                        raise ApiError(400, "invalid_field", "fatal 必须是布尔值")
                    new_fatal = int(item["fatal"])
                if new_fatal and not new_serious:
                    raise ApiError(400, "invalid_severity", "死亡搭配必须标记为严重")
                if (new_serious, new_fatal) != (pairing["serious"], pairing["fatal"]):
                    conn.execute(
                        "UPDATE pairings SET serious=?,fatal=?,anchor_at=?,updated_at=? WHERE id=?",
                        (new_serious, new_fatal, received_iso, now, pairing["id"]),
                    )
                    changed_pairing_ids.add(pairing["id"])

            # 补新搭配；同一案例同一组搭配并发提交只建一份
            for item in new_items:
                if not isinstance(item, dict):
                    raise ApiError(400, "invalid_pairing", "new_pairings 元素必须是对象")
                product = str(item.get("product", "")).strip()
                event_term = str(item.get("event_term", "")).strip()
                if not product or not event_term:
                    raise ApiError(400, "missing_fields", "新搭配的 product 和 event_term 必填")
                new_serious = self._bool_field(item, "serious", False)
                new_fatal = self._bool_field(item, "fatal", False)
                if new_fatal and not new_serious:
                    raise ApiError(400, "invalid_severity", "死亡搭配必须标记为严重")
                product_key, event_key = norm_key(product), norm_key(event_term)
                existing = conn.execute(
                    "SELECT * FROM pairings WHERE case_id=? AND product_key=? AND event_key=?",
                    (case_id, product_key, event_key),
                ).fetchone()
                if existing is not None:
                    if not any(p["id"] == existing["id"] for p in deduplicated):
                        deduplicated.append({"id": existing["id"], "seq": existing["seq"],
                                             "product": existing["product"], "event_term": existing["event_term"]})
                    continue
                next_seq = conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM pairings WHERE case_id=?", (case_id,)
                ).fetchone()[0]
                try:
                    cursor = conn.execute(
                        """INSERT INTO pairings(case_id,seq,product,event_term,product_key,event_key,
                           serious,fatal,causality,anchor_at,created_by,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (case_id, next_seq, product, event_term, product_key, event_key,
                         int(new_serious), int(new_fatal), item.get("causality"),
                         received_iso, actor, now, now),
                    )
                except sqlite3.IntegrityError:
                    # 并发事务抢先插入同一组搭配：只留一份
                    existing = conn.execute(
                        "SELECT * FROM pairings WHERE case_id=? AND product_key=? AND event_key=?",
                        (case_id, product_key, event_key),
                    ).fetchone()
                    if existing is not None and not any(p["id"] == existing["id"] for p in deduplicated):
                        deduplicated.append({"id": existing["id"], "seq": existing["seq"],
                                             "product": existing["product"], "event_term": existing["event_term"]})
                    continue
                created.append({"id": cursor.lastrowid, "seq": next_seq,
                                "product": product, "event_term": event_term})

            # 转归变化的搭配：未交报告期限按本次随访接收时间重算
            recomputed = 0
            for pairing_id in changed_pairing_ids:
                pairing = self._pairing(conn, pairing_id, case_id)
                recomputed += self._recompute_open_reports(conn, pairing, received)

            conn.execute(
                "UPDATE cases SET revision=?,received_at=?,updated_at=? WHERE id=?",
                (revision, received_iso, now, case_id),
            )
            Repository.audit(conn, case_id, actor, role, "followup_added", {
                "revision": revision, "source": source,
                "pairings_created": created, "pairings_deduplicated": deduplicated,
                "pairings_changed": sorted(changed_pairing_ids), "reports_recomputed": recomputed,
            })
            return {
                "case": self.case_view(conn, self._case(conn, case_id)),
                "revision": revision,
                "created_pairings": created,
                "deduplicated_pairings": deduplicated,
                "reports_recomputed": recomputed,
            }

    # ----- 医学裁定：按搭配 -----

    def medical_review(self, case_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "medical_reviewer":
            raise ApiError(403, "medical_reviewer_required", "只有医学审核员可以裁定严重性")
        expected = body.get("expected_revision")
        if not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")
        serious = body.get("serious")
        fatal = body.get("fatal")
        causality = str(body.get("causality", "")).strip()
        rationale = str(body.get("rationale", "")).strip()
        if not isinstance(serious, bool) or not isinstance(fatal, bool) or not causality or not rationale:
            raise ApiError(400, "invalid_review", "serious/fatal 必须是布尔值，causality 和 rationale 必填")
        if fatal and not serious:
            raise ApiError(400, "invalid_severity", "死亡搭配必须标记为严重")
        received = parse_time(body.get("received_at"))
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if case["status"] == "merged":
                raise ApiError(409, "case_merged", "已合并案例不能审核")
            if case["revision"] != expected:
                raise ApiError(409, "revision_conflict", "案例版本已变化")
            pairing = (self._pairing(conn, body["pairing_id"], case_id) if isinstance(body.get("pairing_id"), int)
                       else self._primary_pairing(conn, case_id))
            revision = expected + 1
            conn.execute(
                """INSERT INTO medical_reviews(case_id,pairing_id,case_revision,serious,fatal,causality,
                   rationale,reviewer,created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (case_id, pairing["id"], expected, int(serious), int(fatal), causality,
                 rationale, actor, iso()),
            )
            outcome_changed = (serious, fatal) != (bool(pairing["serious"]), bool(pairing["fatal"]))
            if outcome_changed:
                conn.execute(
                    "UPDATE pairings SET serious=?,fatal=?,causality=?,anchor_at=?,updated_at=? WHERE id=?",
                    (int(serious), int(fatal), causality, iso(received), iso(), pairing["id"]),
                )
                recomputed = self._recompute_open_reports(
                    conn, self._pairing(conn, pairing["id"], case_id), received)
            else:
                conn.execute("UPDATE pairings SET causality=?,updated_at=? WHERE id=?", (causality, iso(), pairing["id"]))
                recomputed = 0
            conn.execute("UPDATE cases SET revision=?,updated_at=? WHERE id=?", (revision, iso(), case_id))
            Repository.audit(conn, case_id, actor, role, "medical_reviewed", {
                "pairing_id": pairing["id"], "from_revision": expected,
                "serious": serious, "fatal": fatal, "causality": causality,
                "outcome_changed": outcome_changed, "reports_recomputed": recomputed,
            })
            return {"case": self.case_view(conn, self._case(conn, case_id)),
                    "pairing_id": pairing["id"], "reviewed_revision": expected,
                    "reports_recomputed": recomputed}

    # ----- 分国家报告（按搭配） -----

    def create_report(self, case_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "report_forbidden", "只有区域负责人或全局管理员可以生成报告")
        country = str(body.get("country", "")).strip().upper()
        if not country:
            raise ApiError(400, "country_required", "country 必填")
        with self.repo.tx() as conn:
            case = self._case(conn, case_id)
            if not self.can_access(case, role, region):
                raise ApiError(403, "region_forbidden", "不能为本区域之外案例生成报告")
            pairing = (self._pairing(conn, body["pairing_id"], case_id)
                       if isinstance(body.get("pairing_id"), int)
                       else self._primary_pairing(conn, case_id))
            due = report_deadline(parse_time(pairing["anchor_at"]), bool(pairing["serious"]), bool(pairing["fatal"]))
            try:
                cur = conn.execute(
                    "INSERT INTO reports(case_id,pairing_id,country,due_at,status) VALUES(?,?,?,?, 'pending')",
                    (case_id, pairing["id"], country, iso(due)),
                )
            except sqlite3.IntegrityError as exc:
                raise ApiError(409, "report_exists", "该国家针对该搭配的报告已经存在") from exc
            Repository.audit(conn, case_id, actor, role, "report_created",
                             {"report_id": cur.lastrowid, "country": country, "pairing_id": pairing["id"],
                              "product": pairing["product"], "event_term": pairing["event_term"]})
            return dict(conn.execute("SELECT * FROM reports WHERE id=?", (cur.lastrowid,)).fetchone())

    def submit_report(self, report_id: int, actor: str, role: str, region: str, body: dict[str, Any]) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "submit_forbidden", "当前角色不能提交监管报告")
        with self.repo.tx() as conn:
            row = conn.execute(
                "SELECT r.*,c.region FROM reports r JOIN cases c ON c.id=r.case_id WHERE r.id=?",
                (report_id,)).fetchone()
            if not row:
                raise ApiError(404, "report_not_found", "报告不存在")
            if not self.can_access(dict(row), role, region):
                raise ApiError(403, "region_forbidden", "无权提交其他区域报告")
            if row["status"] == "submitted":
                return {"report": dict(row), "idempotent": True}
            now = parse_time(body.get("submitted_at"), utcnow())
            late = int(now > parse_time(row["due_at"]))
            conn.execute(
                "UPDATE reports SET status='submitted',submitted_at=?,submitted_by=?,late=? WHERE id=?",
                (iso(now), actor, late, report_id))
            Repository.audit(conn, row["case_id"], actor, role, "report_submitted",
                             {"report_id": report_id, "country": row["country"],
                              "pairing_id": row["pairing_id"], "late": bool(late)})
            return {"report": dict(conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()),
                    "idempotent": False}

    # ----- 合并：同患者案例的多组搭配取并集 -----

    def merge_cases(self, source_id: int, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        if role != "global_admin":
            raise ApiError(403, "merge_forbidden", "只有全局管理员可以合并案例")
        target_id = body.get("target_case_id")
        if not isinstance(target_id, int) or source_id == target_id:
            raise ApiError(400, "invalid_target", "target_case_id 必须指向不同案例")
        with self.repo.tx() as conn:
            source = self._case(conn, source_id)
            target = self._case(conn, target_id)
            if source["status"] == "merged":
                return {"case": self.case_view(conn, source), "idempotent": True}
            if target["status"] == "merged":
                raise ApiError(409, "merge_conflict", "目标案例已合并，不可用")
            if norm_key(source["patient_ref"]) != norm_key(target["patient_ref"]):
                raise ApiError(409, "merge_conflict", "患者不一致的案例不能合并")

            target_pairings = {(p["product_key"], p["event_key"]): p
                               for p in self._pairings(conn, target_id)}
            next_seq = max(target_pairings.values(), key=lambda p: p["seq"])["seq"] if target_pairings else 0
            merged_pairings: list[dict[str, Any]] = []
            for source_pairing in self._pairings(conn, source_id):
                key = (source_pairing["product_key"], source_pairing["event_key"])
                target_pairing = target_pairings.get(key)
                if target_pairing is None:
                    # 目标案例没有的搭配：整体并入并顺延序号
                    next_seq += 1
                    conn.execute("UPDATE pairings SET case_id=?,seq=?,updated_at=? WHERE id=?",
                                 (target_id, next_seq, iso(), source_pairing["id"]))
                    conn.execute("UPDATE reports SET case_id=? WHERE pairing_id=?",
                                 (target_id, source_pairing["id"]))
                    continue
                # 同一组搭配：报告按国家归并，同一国家只留一份（优先已提交，否则最早）
                for source_report in conn.execute(
                        "SELECT * FROM reports WHERE pairing_id=?", (source_pairing["id"],)):
                    clash = conn.execute(
                        "SELECT * FROM reports WHERE pairing_id=? AND country=?",
                        (target_pairing["id"], source_report["country"])).fetchone()
                    if clash is None:
                        conn.execute("UPDATE reports SET case_id=?,pairing_id=? WHERE id=?",
                                     (target_id, target_pairing["id"], source_report["id"]))
                        continue
                    candidates = [clash, source_report]
                    winner = max(candidates, key=lambda r: (r["status"] == "submitted", -r["id"]))
                    loser = clash if winner is source_report else source_report
                    conn.execute("UPDATE reports SET case_id=?,pairing_id=? WHERE id=?",
                                 (target_id, target_pairing["id"], winner["id"]))
                    conn.execute("DELETE FROM reports WHERE id=?", (loser["id"],))
                    Repository.audit(conn, target_id, actor, role, "report_deduped_on_merge", {
                        "kept_report_id": winner["id"], "dropped_report_id": loser["id"],
                        "country": winner["country"], "pairing_id": target_pairing["id"]})
                # 同搭配的旧审核记录并入；同一版本号已有裁定则保留目标侧记录
                for source_review in conn.execute(
                        "SELECT * FROM medical_reviews WHERE pairing_id=?", (source_pairing["id"],)):
                    review_clash = conn.execute(
                        "SELECT 1 FROM medical_reviews WHERE pairing_id=? AND case_revision=?",
                        (target_pairing["id"], source_review["case_revision"])).fetchone()
                    if review_clash is None:
                        conn.execute("UPDATE medical_reviews SET case_id=?,pairing_id=? WHERE id=?",
                                     (target_id, target_pairing["id"], source_review["id"]))
                    else:
                        conn.execute("DELETE FROM medical_reviews WHERE id=?", (source_review["id"],))
                conn.execute("DELETE FROM pairings WHERE id=?", (source_pairing["id"],))
                merged_pairings.append({"source_pairing_id": source_pairing["id"],
                                        "target_pairing_id": target_pairing["id"]})

            conn.execute("UPDATE intakes SET case_id=? WHERE case_id=?", (target_id, source_id))
            conn.execute("UPDATE followups SET case_id=? WHERE case_id=?", (target_id, source_id))
            conn.execute("UPDATE medical_reviews SET case_id=? WHERE case_id=?", (target_id, source_id))
            conn.execute("UPDATE cases SET status='merged',merged_into=?,revision=revision+1,updated_at=? WHERE id=?",
                         (target_id, iso(), source_id))
            Repository.audit(conn, target_id, actor, role, "case_merged_in",
                             {"source_case_id": source_id, "overlapping_pairings": merged_pairings})
            Repository.audit(conn, source_id, actor, role, "case_merged_into", {"target_case_id": target_id})
            return {"case": self.case_view(conn, self._case(conn, source_id)), "idempotent": False}

    # ----- 逾期 -----

    def overdue(self, role: str, region: str) -> list[dict[str, Any]]:
        sql = """SELECT r.* FROM reports r WHERE r.status!='submitted' AND r.due_at < ?"""
        args: list[Any] = [iso()]
        if role not in {"medical_reviewer", "global_admin"}:
            sql += " AND r.case_id IN (SELECT id FROM cases WHERE region=?)"
            args.append(region)
        return [dict(r) for r in self.repo.conn.execute(sql, args)]

    def escalate_overdue(self, actor: str, role: str, region: str) -> dict[str, Any]:
        if role not in {"regional_lead", "global_admin"}:
            raise ApiError(403, "escalation_forbidden", "当前角色不能执行逾期升级")
        rows = self.overdue(role, region)
        with self.repo.tx() as conn:
            for row in rows:
                conn.execute("UPDATE reports SET status='overdue' WHERE id=? AND status='pending'", (row["id"],))
                Repository.audit(conn, row["case_id"], actor, role, "report_overdue_escalated",
                                 {"report_id": row["id"], "country": row["country"]})
        return {"escalated": len(rows)}

    def state(self, role: str, region: str) -> dict[str, Any]:
        cases = self.list_cases(role, region, {})
        return {"cases": cases, "overdue": self.overdue(role, region), "server_time": iso()}


def json_response(handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
    raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class Handler(BaseHTTPRequestHandler):
    service: PharmacovigilanceService
    web_root: Path

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            result = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", "请求体不是有效 JSON") from exc
        if not isinstance(result, dict):
            raise ApiError(400, "invalid_json", "请求体必须是 JSON 对象")
        return result

    def _dispatch_get(self, path: str, query: dict[str, list[str]]) -> Any:
        if path == "/health":
            return 200, {"status": "ok", "service": "pharmacovigilance"}
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/state":
            return 200, self.service.state(role, region)
        if path == "/api/cases":
            return 200, {"cases": self.service.list_cases(role, region, query)}
        if path == "/api/overdue":
            return 200, {"reports": self.service.overdue(role, region)}
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            return 200, self.service.get_case(int(parts[2]), role, region)
        raise ApiError(404, "not_found", "接口不存在")

    def _dispatch_post(self, path: str, body: dict[str, Any]) -> Any:
        actor, role, region = self.service.identity(self.headers)
        if path == "/api/cases":
            return 201, self.service.create_case(actor, role, region, body)
        if path == "/api/escalate-overdue":
            return 200, self.service.escalate_overdue(actor, role, region)
        parts = [part for part in path.split("/") if part]
        if len(parts) == 4 and parts[:2] == ["api", "cases"] and parts[2].isdigit():
            case_id, action = int(parts[2]), parts[3]
            if action == "followups":
                return 201, self.service.add_followup(case_id, actor, role, region, body)
            if action == "medical-review":
                return 200, self.service.medical_review(case_id, actor, role, body)
            if action == "reports":
                return 201, self.service.create_report(case_id, actor, role, region, body)
            if action == "merge":
                return 200, self.service.merge_cases(case_id, actor, role, body)
        if len(parts) == 4 and parts[:2] == ["api", "reports"] and parts[2].isdigit() and parts[3] == "submit":
            return 200, self.service.submit_report(int(parts[2]), actor, role, region, body)
        raise ApiError(404, "not_found", "接口不存在")

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        try:
            if method == "GET" and parsed.path == "/":
                page = (self.web_root / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
                return
            if method == "GET":
                status, payload = self._dispatch_get(parsed.path, parse_qs(parsed.query))
            else:
                status, payload = self._dispatch_post(parsed.path, self._body())
            json_response(self, status, payload)
        except ApiError as exc:
            json_response(self, exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:
            print(f"unhandled error: {exc!r}")
            json_response(self, 500, {"error": "internal_error", "message": str(exc)})

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")


def create_server(db_path: str | Path, host: str = "127.0.0.1", port: int = PORT) -> ThreadingHTTPServer:
    service = PharmacovigilanceService(db_path)
    web_root = Path(__file__).resolve().parent / "static"
    handler = type("PharmacovigilanceHandler", (Handler,), {"service": service, "web_root": web_root})
    return ThreadingHTTPServer((host, port), handler)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--db", default=os.environ.get("PV_DB", "pharmacovigilance.db"))
    args = parser.parse_args()
    server = create_server(args.db, args.host, args.port)
    print(f"pharmacovigilance listening on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
