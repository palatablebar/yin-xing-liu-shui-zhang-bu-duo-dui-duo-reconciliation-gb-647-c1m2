"""SQLite 存储：批次、流水行、候选、匹配。

约定：
- 金额与手续费一律存 Decimal 字符串，不落 float；
- 一次运行的所有行在同一个事务里写入，失败整体回滚；
- 同一 run_id 重跑先删后插，行数、合计与首跑一致；
- 大整数编号（流水号、凭证号）按字符串保形，不过 int()。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .manifest import utc_now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}',
    bank_sha256 TEXT NOT NULL DEFAULT '',
    ledger_sha256 TEXT NOT NULL DEFAULT '',
    bank_file TEXT NOT NULL DEFAULT '',
    ledger_file TEXT NOT NULL DEFAULT '',
    bank_bytes INTEGER NOT NULL DEFAULT 0,
    ledger_bytes INTEGER NOT NULL DEFAULT 0,
    bank_raw INTEGER NOT NULL DEFAULT 0,
    ledger_raw INTEGER NOT NULL DEFAULT 0,
    bank_valid INTEGER NOT NULL DEFAULT 0,
    ledger_valid INTEGER NOT NULL DEFAULT 0,
    bank_quar INTEGER NOT NULL DEFAULT 0,
    ledger_quar INTEGER NOT NULL DEFAULT 0,
    segments INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'empty',
    input_states_json TEXT NOT NULL DEFAULT '{}',
    duration_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS records (
    run_id TEXT NOT NULL,
    side TEXT NOT NULL,
    seq INTEGER NOT NULL,
    row_no INTEGER NOT NULL,
    txn_no TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    posted_at TEXT NOT NULL DEFAULT '',
    direction INTEGER NOT NULL DEFAULT 0,
    amount TEXT NOT NULL DEFAULT '',
    fee TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT '',
    counterparty TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'valid',
    reason TEXT NOT NULL DEFAULT '',
    flags_json TEXT NOT NULL DEFAULT '[]',
    raw_json TEXT NOT NULL DEFAULT '[]',
    raw_amount TEXT NOT NULL DEFAULT '',
    raw_date TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id, side, status);
CREATE TABLE IF NOT EXISTS candidates (
    run_id TEXT NOT NULL,
    cand_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_candidates_run ON candidates(run_id, tier);
CREATE TABLE IF NOT EXISTS matches (
    run_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    bank_ids TEXT NOT NULL,
    ledger_ids TEXT NOT NULL,
    bank_sum TEXT NOT NULL,
    ledger_sum TEXT NOT NULL,
    diff TEXT NOT NULL,
    day_gap INTEGER NOT NULL DEFAULT 0,
    fee_alloc TEXT NOT NULL DEFAULT '0',
    fee_actual TEXT NOT NULL DEFAULT '0',
    bank_signed TEXT NOT NULL DEFAULT '0',
    ledger_signed TEXT NOT NULL DEFAULT '0'
);
CREATE INDEX IF NOT EXISTS idx_matches_run ON matches(run_id, tier);
CREATE TABLE IF NOT EXISTS posting_state (
    run_id TEXT PRIMARY KEY,
    scheme_version TEXT NOT NULL DEFAULT '',
    journal_count INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS journals (
    run_id TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    match_id TEXT NOT NULL,
    tier INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    bank_refs TEXT NOT NULL DEFAULT '[]',
    ledger_refs TEXT NOT NULL DEFAULT '[]',
    debit_total TEXT NOT NULL DEFAULT '0',
    credit_total TEXT NOT NULL DEFAULT '0',
    amount_total TEXT NOT NULL DEFAULT '0',
    posted_at TEXT NOT NULL DEFAULT '',
    scheme_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, journal_id)
);
CREATE INDEX IF NOT EXISTS idx_journals_run ON journals(run_id, tier);
CREATE TABLE IF NOT EXISTS journal_lines (
    run_id TEXT NOT NULL,
    journal_id TEXT NOT NULL,
    line_no INTEGER NOT NULL,
    line_id TEXT NOT NULL,
    line_type TEXT NOT NULL,
    subject_code TEXT NOT NULL DEFAULT '',
    subject_name TEXT NOT NULL DEFAULT '',
    debit TEXT NOT NULL DEFAULT '0',
    credit TEXT NOT NULL DEFAULT '0',
    signed TEXT NOT NULL DEFAULT '0',
    ref_side TEXT NOT NULL DEFAULT '',
    refs_json TEXT NOT NULL DEFAULT '[]',
    memo TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, line_id)
);
CREATE INDEX IF NOT EXISTS idx_journal_lines_run ON journal_lines(run_id, journal_id, line_no);
"""

RUN_COLUMNS = (
    "run_id", "created_at", "rule_version", "params_json", "bank_sha256", "ledger_sha256",
    "bank_file", "ledger_file", "bank_bytes", "ledger_bytes", "bank_raw", "ledger_raw",
    "bank_valid", "ledger_valid", "bank_quar", "ledger_quar", "segments", "status",
    "input_states_json", "duration_ms",
)


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    # ---------- 批次 ----------
    @staticmethod
    def _run_row(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["params"] = json.loads(data.pop("params_json") or "{}")
        data["input_states"] = json.loads(data.pop("input_states_json") or "{}")
        return data

    def latest_run(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        return self._run_row(row) if row else None

    def get_run(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._run_row(row) if row else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._run_row(row) for row in rows]

    def save_run(self, run: dict, records: list[dict], candidates: list[dict],
                 matches: list[dict], journals: list[dict] | None = None,
                 posting_scheme: str = "") -> None:
        params_json = json.dumps(run.get("params", {}), ensure_ascii=False, sort_keys=True)
        states_json = json.dumps(run.get("input_states", {}), ensure_ascii=False)
        journals = journals or []
        with self._conn() as conn:
            run_id = run["run_id"]
            conn.execute("DELETE FROM records WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            self._replace_postings(conn, run_id, journals, posting_scheme)
            conn.execute(
                """INSERT OR REPLACE INTO runs (
                    run_id, created_at, rule_version, params_json, bank_sha256, ledger_sha256,
                    bank_file, ledger_file, bank_bytes, ledger_bytes, bank_raw, ledger_raw,
                    bank_valid, ledger_valid, bank_quar, ledger_quar, segments, status,
                    input_states_json, duration_ms
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, run["created_at"], run["rule_version"], params_json,
                    run.get("bank_sha256", ""), run.get("ledger_sha256", ""),
                    run.get("bank_file", ""), run.get("ledger_file", ""),
                    run.get("bank_bytes", 0), run.get("ledger_bytes", 0),
                    run.get("bank_raw", 0), run.get("ledger_raw", 0),
                    run.get("bank_valid", 0), run.get("ledger_valid", 0),
                    run.get("bank_quar", 0), run.get("ledger_quar", 0),
                    run.get("segments", 0), run.get("status", "empty"),
                    states_json, run.get("duration_ms", 0),
                ),
            )
            conn.executemany(
                """INSERT INTO records (run_id, side, seq, row_no, txn_no, account, posted_at,
                    direction, amount, fee, memo, counterparty, status, reason, flags_json,
                    raw_json, raw_amount, raw_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, r["side"], r["seq"], r["row_no"], r["txn_no"], r.get("account", ""),
                        r.get("posted_at", ""), r.get("direction", 0), str(r.get("amount", "")),
                        str(r.get("fee", "")), r.get("memo", ""), r.get("counterparty", ""),
                        r.get("status", "valid"), r.get("reason", ""),
                        json.dumps(r.get("flags", []), ensure_ascii=False),
                        json.dumps(r.get("raw", []), ensure_ascii=False),
                        str(r.get("raw_amount", "")), str(r.get("raw_date", "")),
                    )
                    for r in records
                ],
            )
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    def set_status(self, run_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))

    # ---------- 行 ----------
    def records(self, run_id: str, side: str | None = None,
                status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM records WHERE run_id = ?"
        args: list = [run_id]
        if side:
            sql += " AND side = ?"
            args.append(side)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY side, row_no, seq"
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["flags"] = json.loads(item.pop("flags_json") or "[]")
            item["raw"] = json.loads(item.pop("raw_json") or "[]")
            out.append(item)
        return out

    def quarantine(self, run_id: str) -> list[dict]:
        return self.records(run_id, status="quarantine")

    def candidates(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM candidates WHERE run_id = ? ORDER BY tier, cand_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def matches(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM matches WHERE run_id = ? ORDER BY tier, match_id", (run_id,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bank_ids"] = json.loads(item.pop("bank_ids") or "[]")
            item["ledger_ids"] = json.loads(item.pop("ledger_ids") or "[]")
            out.append(item)
        return out

    def replace_candidates(self, run_id: str, candidates: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM candidates WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO candidates (run_id, cand_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap) VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, c["cand_id"], c["tier"],
                        json.dumps(c["bank_ids"], ensure_ascii=False),
                        json.dumps(c["ledger_ids"], ensure_ascii=False),
                        str(c["bank_sum"]), str(c["ledger_sum"]), str(c["diff"]), c["day_gap"],
                    )
                    for c in candidates
                ],
            )

    def replace_matches(self, run_id: str, matches: list[dict]) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )

    # ---------- 入账凭证 ----------
    @staticmethod
    def _replace_postings(conn, run_id: str, journals: list[dict],
                          scheme_version: str) -> None:
        """在调用方事务内整体替换入账凭证：先删后插，失败随事务回滚。

        同一 run_id 重跑命中同一批主键，不会多出凭证/分录；三张表同事务，
        中断时整体回滚，旧版完整方案仍在（或整批缺失可按确定性方案重建）。
        """
        conn.execute("DELETE FROM journal_lines WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM journals WHERE run_id = ?", (run_id,))
        conn.execute("DELETE FROM posting_state WHERE run_id = ?", (run_id,))
        line_count = 0
        for journal in journals:
            conn.execute(
                """INSERT INTO journals (run_id, journal_id, match_id, tier, line_count,
                    bank_refs, ledger_refs, debit_total, credit_total, amount_total,
                    posted_at, scheme_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, journal["journal_id"], journal["match_id"], journal["tier"],
                    journal["line_count"],
                    json.dumps(journal["bank_refs"], ensure_ascii=False),
                    json.dumps(journal["ledger_refs"], ensure_ascii=False),
                    str(journal["debit_total"]), str(journal["credit_total"]),
                    str(journal["amount_total"]), journal.get("posted_at", ""),
                    journal.get("scheme_version", scheme_version),
                ),
            )
            for line in journal["lines"]:
                line_count += 1
                conn.execute(
                    """INSERT INTO journal_lines (run_id, journal_id, line_no, line_id,
                        line_type, subject_code, subject_name, debit, credit, signed,
                        ref_side, refs_json, memo)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, journal["journal_id"], line["line_no"], line["line_id"],
                        line["line_type"], line["subject_code"], line["subject_name"],
                        str(line["debit"]), str(line["credit"]), str(line["signed"]),
                        line.get("ref_side", ""),
                        json.dumps(line["refs"], ensure_ascii=False),
                        line.get("memo", ""),
                    ),
                )
        conn.execute(
            """INSERT INTO posting_state (run_id, scheme_version, journal_count,
                line_count, updated_at) VALUES (?,?,?,?,?)""",
            (
                run_id, scheme_version, len(journals), line_count,
                utc_now_iso(),
            ),
        )

    def replace_postings(self, run_id: str, journals: list[dict],
                         scheme_version: str) -> None:
        with self._conn() as conn:
            self._replace_postings(conn, run_id, journals, scheme_version)

    def replace_matches_and_postings(self, run_id: str, matches: list[dict],
                                     journals: list[dict],
                                     scheme_version: str) -> None:
        """匹配重求解与入账重建放同一事务，避免凭证指向已失效的匹配组。"""
        with self._conn() as conn:
            conn.execute("DELETE FROM matches WHERE run_id = ?", (run_id,))
            conn.executemany(
                """INSERT INTO matches (run_id, match_id, tier, bank_ids, ledger_ids,
                    bank_sum, ledger_sum, diff, day_gap, fee_alloc, fee_actual,
                    bank_signed, ledger_signed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        run_id, m["match_id"], m["tier"],
                        json.dumps(m["bank_ids"], ensure_ascii=False),
                        json.dumps(m["ledger_ids"], ensure_ascii=False),
                        str(m["bank_sum"]), str(m["ledger_sum"]), str(m["diff"]), m["day_gap"],
                        str(m.get("fee_alloc", "0")), str(m.get("fee_actual", "0")),
                        str(m.get("bank_signed", "0")), str(m.get("ledger_signed", "0")),
                    )
                    for m in matches
                ],
            )
            self._replace_postings(conn, run_id, journals, scheme_version)

    def posting_state(self, run_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM posting_state WHERE run_id = ?", (run_id,)
            ).fetchone()
        return dict(row) if row else None

    def journals(self, run_id: str) -> list[dict]:
        with self._conn() as conn:
            journal_rows = conn.execute(
                "SELECT * FROM journals WHERE run_id = ? ORDER BY tier, journal_id", (run_id,)
            ).fetchall()
            line_rows = conn.execute(
                "SELECT * FROM journal_lines WHERE run_id = ? ORDER BY journal_id, line_no",
                (run_id,),
            ).fetchall()
        lines_by_journal: dict[str, list[dict]] = {}
        for row in line_rows:
            item = dict(row)
            item["refs"] = json.loads(item.pop("refs_json") or "[]")
            lines_by_journal.setdefault(item["journal_id"], []).append(item)
        out = []
        for row in journal_rows:
            journal = dict(row)
            journal["bank_refs"] = json.loads(journal.pop("bank_refs") or "[]")
            journal["ledger_refs"] = json.loads(journal.pop("ledger_refs") or "[]")
            journal["lines"] = lines_by_journal.get(journal["journal_id"], [])
            out.append(journal)
        return out
