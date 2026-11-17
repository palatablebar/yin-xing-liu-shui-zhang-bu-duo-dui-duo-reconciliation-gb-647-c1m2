"""M8 入账：完整方案、借贷平衡、可追溯、不轧差、幂等、中断恢复、页面/导出一致。"""
import csv
import io
import sqlite3
from decimal import Decimal

from fastapi.testclient import TestClient

from app.config import (
    CLEARING_SUBJECT,
    FEE_EXPENSE_SUBJECT,
    GAP_LOSS_SUBJECT,
    POSTING_SCHEME_VERSION,
)
from app.modules.m8_posting import posting
from app.modules.m8_posting.posting import (
    LINE_BANK,
    LINE_FEE_CASH,
    LINE_FEE_EXPENSE,
    LINE_GAP,
    LINE_VOUCHER,
)


def postings(client):
    return client.get("/api/postings").json()


def journals(client):
    return postings(client)["journals"]


def lines(client):
    return [line for journal in journals(client) for line in journal["lines"]]


def test_posting_scheme_version_single_source(client, demo):
    data = postings(client)
    assert data["scheme_version"] == POSTING_SCHEME_VERSION
    assert data["stale"] is False
    assert data["stored_scheme_version"] == POSTING_SCHEME_VERSION
    # 每个匹配组一张凭证，凭证与匹配组一一对应。
    matches = client.get("/api/matches").json()["groups"]
    assert data["journal_count"] == len(matches) == 11
    assert {j["match_id"] for j in data["journals"]} == {m["match_id"] for m in matches}


def test_every_journal_balances(client, demo):
    for journal in journals(client):
        assert Decimal(journal["debit_total"]) == Decimal(journal["credit_total"])
        signed_sum = sum((Decimal(line["signed"]) for line in journal["lines"]), Decimal("0"))
        assert signed_sum == Decimal("0.00")
    data = postings(client)
    assert Decimal(data["debit_total"]) == Decimal(data["credit_total"])


def test_one_bank_payment_merges_vouchers_but_keeps_each_ref(client, demo):
    # 一对多：一笔银行付款对应多张凭证，合并成一条待核销往来分录，
    # 但每张被核销凭证号都必须保留（净额不能掩盖核销对象）。
    target = next(j for j in journals(client) if j["tier"] == 2
                  and j["bank_refs"] == ["000002"])
    voucher_lines = [ln for ln in target["lines"] if ln["line_type"] == LINE_VOUCHER]
    assert len(voucher_lines) == 1
    assert voucher_lines[0]["refs"] == ["V002", "V003"]
    assert Decimal(voucher_lines[0]["debit"]) == Decimal("3000.00")
    # 银行流水号同样逐笔保留。
    bank_lines = [ln for ln in target["lines"] if ln["line_type"] == LINE_BANK]
    assert bank_lines[0]["refs"] == ["000002"]


def test_line_amount_equals_sum_of_referenced_source_rows(client, demo):
    # 反净额掩盖：每条分录金额必须等于其引用原始对象金额之和，可逐对象追到最终科目。
    store = client.app.state.store
    run_id = demo["run_id"]
    records = {r["txn_no"]: r for r in store.records(run_id) if r["status"] == "valid"}
    for journal in journals(client):
        for line in journal["lines"]:
            if line["line_type"] in (LINE_FEE_EXPENSE, LINE_GAP):
                continue
            expected = Decimal("0.00")
            for ref in line["refs"]:
                row = records[ref]
                if line["line_type"] == LINE_FEE_CASH:
                    # 手续费现金固定为贷方支出，签向取负。
                    expected -= Decimal(row["fee"])
                else:
                    expected += Decimal(row["amount"]) * int(row["direction"])
            booked = Decimal(line["debit"]) - Decimal(line["credit"])
            # 凭证分录是反方向核销。
            if line["line_type"] == LINE_VOUCHER:
                expected = -expected
            assert booked == expected, (journal["journal_id"], line["line_id"])


def test_every_source_id_is_traceable_to_a_subject(client, demo):
    # 审计要求：原始流水和凭证都能追到最终科目——没有一个匹配组内对象缺席。
    all_refs = {ref for line in lines(client) for ref in line["refs"]}
    groups = client.get("/api/matches").json()["groups"]
    for group in groups:
        for ref in group["bank_ids"] + group["ledger_ids"]:
            assert ref in all_refs
    # 每条分录都落到具体科目，不存在无科目分录。
    assert all(line["subject_code"] and line["subject_name"] for line in lines(client))


def test_fee_booked_as_separate_balanced_pair_not_netted(client, demo):
    target = next(j for j in journals(client) if j["bank_refs"] == ["000003"])
    cash = next(ln for ln in target["lines"] if ln["line_type"] == LINE_FEE_CASH)
    expense = next(ln for ln in target["lines"] if ln["line_type"] == LINE_FEE_EXPENSE)
    assert cash["subject_code"].startswith("1002")
    assert Decimal(cash["credit"]) == Decimal("1.50")
    assert expense["subject_code"] == FEE_EXPENSE_SUBJECT[0]
    assert Decimal(expense["debit"]) == Decimal("1.50")
    # 手续费不允许并进银行流水本金分录。
    bank_line = next(ln for ln in target["lines"] if ln["line_type"] == LINE_BANK)
    assert Decimal(bank_line["credit"]) == Decimal("1250.50")
    # 全批手续费费用合计等于实际手续费 22.31。
    fee_expense_total = sum(
        (Decimal(ln["debit"]) for ln in lines(client)
         if ln["line_type"] == LINE_FEE_EXPENSE),
        Decimal("0.00"),
    )
    assert fee_expense_total == Decimal("22.31")


def test_tolerance_gap_posted_to_gap_subject(client, demo):
    # 组内容差尾差单独挂科目，不靠轧平掩盖；金额落在容差内。
    gap_lines = [ln for ln in lines(client) if ln["line_type"] == LINE_GAP]
    assert gap_lines
    for line in gap_lines:
        assert line["subject_code"] == GAP_LOSS_SUBJECT[0]
        assert abs(Decimal(line["debit"]) - Decimal(line["credit"])) <= Decimal("0.02")
        # 尾差分录同时引用银行与账簿两侧对象。
        assert line["ref_side"] == "both" and len(line["refs"]) >= 2


def test_small_number_of_lines_per_journal(client, demo):
    # 财务诉求：少量分录——每凭证最多 5 条（银行/往来/手续费现金/费用/尾差）。
    assert all(2 <= len(journal["lines"]) <= 5 for journal in journals(client))
    # 多对一 4 笔银行流水也只合并成一条银行存款分录。
    target = next(j for j in journals(client) if j["tier"] == 3
                  and len(journal_bank_refs(j)) == 4)
    bank_lines = [ln for ln in target["lines"] if ln["line_type"] == LINE_BANK]
    assert len(bank_lines) == 1
    assert len(bank_lines[0]["refs"]) == 4


def journal_bank_refs(journal):
    return journal["bank_refs"]


def test_rerun_is_idempotent_no_extra_or_missing_rows(client, demo):
    before = postings(client)
    client.post("/api/demo/run")  # 同一 run_id 重跑
    after = postings(client)
    assert after["run_id"] == before["run_id"]
    assert after["journal_count"] == before["journal_count"]
    assert after["line_count"] == before["line_count"]
    assert [j["journal_id"] for j in after["journals"]] == \
        [j["journal_id"] for j in before["journals"]]
    # 逐行完全一致（含科目、借贷、来源号）。
    def snapshot(payload):
        return [
            (j["journal_id"], ln["line_no"], ln["line_type"], ln["subject_code"],
             ln["debit"], ln["credit"], tuple(ln["refs"]))
            for j in payload["journals"] for ln in j["lines"]
        ]
    assert snapshot(after) == snapshot(before)
    # 数据库物理行数也不翻倍。
    with sqlite3.connect(client.app.state.settings.db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM journal_lines WHERE run_id = ?", (demo["run_id"],)
        ).fetchone()[0]
    assert rows == before["line_count"]


def test_matches_rerun_rebuilds_postings_in_same_scheme(client, demo):
    # 单步重跑匹配后，入账凭证同步重建，不残留指向旧匹配组的凭证。
    response = client.post("/api/matches")
    assert response.status_code == 200
    data = postings(client)
    assert data["stored_scheme_version"] == POSTING_SCHEME_VERSION
    stored = {j["match_id"] for j in data["journals"]}
    live = {g["match_id"] for g in client.get("/api/matches").json()["groups"]}
    assert stored == live


def test_interrupted_write_keeps_old_complete_scheme(client, demo, monkeypatch):
    # 模拟入账写入在事务中途失败：旧版完整方案必须原样保留，不出现半成品。
    store = client.app.state.store
    before = store.journals(demo["run_id"])
    state_before = store.posting_state(demo["run_id"])

    import pytest
    import app.store as store_mod

    def failing(self_arg, conn, run_id, journals, scheme_version):
        # 先写入半批数据再抛错，验证事务回滚不会留下半成品。
        conn.execute(
            """INSERT INTO journal_lines (run_id, journal_id, line_no, line_id,
                line_type, subject_code, subject_name)
            VALUES (?, 'jrnl-half', 1, 'jrnl-half-L01', 'bank', 'x', '半成品')""",
            (run_id,),
        )
        raise RuntimeError("模拟中断")

    monkeypatch.setattr(store_mod.Store, "_replace_postings", failing)
    # TestClient 默认把服务端异常抛出，等价于请求 500、事务已回滚。
    with pytest.raises(RuntimeError, match="模拟中断"):
        client.post("/api/postings")
    monkeypatch.undo()

    # 中断后旧版完整方案仍在，行数与内容不丢不重。
    after = store.journals(demo["run_id"])
    assert [j["journal_id"] for j in after] == [j["journal_id"] for j in before]
    assert sum(len(j["lines"]) for j in after) == state_before["line_count"]
    assert store.posting_state(demo["run_id"])["scheme_version"] == \
        state_before["scheme_version"]
    # 恢复接口可重建出同一版方案。
    recovered = client.post("/api/postings/recover").json()
    assert recovered["journal_count"] == len(before)
    assert recovered["stale"] is False


def test_legacy_scheme_flagged_stale_and_recoverable(client, demo):
    # 旧版方案（不同 scheme_version）标记 stale，恢复后回到当前完整方案且不丢分录。
    store = client.app.state.store
    with store._conn() as conn:
        conn.execute(
            "UPDATE posting_state SET scheme_version = ? WHERE run_id = ?",
            ("posting-2019.old", demo["run_id"]),
        )
        conn.execute(
            "UPDATE journals SET scheme_version = ? WHERE run_id = ?",
            ("posting-2019.old", demo["run_id"]),
        )
    assert postings(client)["stale"] is True
    recovered = client.post("/api/postings/recover").json()
    assert recovered["stale"] is False
    assert recovered["stored_scheme_version"] == POSTING_SCHEME_VERSION
    assert recovered["line_count"] == postings(client)["line_count"]


def test_page_and_csv_export_use_identical_scheme_and_rows(client, demo):
    page = postings(client)
    csv_text = client.get("/api/export/postings.csv").text
    reader = list(csv.DictReader(io.StringIO(csv_text.lstrip("﻿"))))
    # 行数与页面分录数一致，凭证号、科目、借贷、来源号逐行相同。
    assert len(reader) == page["line_count"]
    page_rows = [
        (j["journal_id"], str(ln["line_no"]), ln["subject_code"],
         ln["debit"], ln["credit"], " ".join(ln["refs"]))
        for j in page["journals"] for ln in j["lines"]
    ]
    csv_rows = [
        (row["凭证号"], row["行号"], row["科目编码"],
         row["借方金额"], row["贷方金额"], row["原始流水/凭证"])
        for row in reader
    ]
    assert csv_rows == page_rows
    assert {row["方案版本"] for row in reader} == {POSTING_SCHEME_VERSION}


def test_manifest_carries_posting_scheme_and_counts(client, demo):
    body = client.get("/api/export/manifest.json").json()
    assert body["posting"]["scheme_version"] == POSTING_SCHEME_VERSION
    assert body["posting"]["stale"] is False
    assert body["posting"]["journal_count"] == 11
    assert body["posting"]["line_count"] == postings(client)["line_count"]
    # 清单里的来源引用计数覆盖全部银行+凭证对象。
    groups = client.get("/api/matches").json()["groups"]
    source_total = sum(len(g["bank_ids"]) + len(g["ledger_ids"]) for g in groups)
    assert body["posting"]["source_ref_count"] >= source_total


def test_posting_endpoints_without_run_404(client):
    # 新应用无批次：空库返回空清单，写操作与导出 404。
    import tempfile
    from pathlib import Path
    from app.main import create_app
    from app import config as config_mod
    with tempfile.TemporaryDirectory() as tmp:
        settings = config_mod.Settings(
            db_path=str(Path(tmp) / "empty.sqlite"),
            sample_dir=client.app.state.settings.sample_dir,
        )
        empty = TestClient(create_app(settings))
        assert empty.get("/api/postings").json()["journals"] == []
        assert empty.post("/api/postings").status_code == 404
        assert empty.post("/api/postings/recover").status_code == 404
        assert empty.get("/api/export/postings.csv").status_code == 404


def test_posting_page_renders(client, demo):
    response = client.get("/postings")
    assert response.status_code == 200
    assert POSTING_SCHEME_VERSION in response.text
    assert "待核销往来" in response.text


def test_partial_run_has_empty_postings_not_garbage(client, settings):
    # 只有一侧数据时不生成凭证，状态行仍登记方案版本以便后续一致重建。
    from tests.conftest import read_sample
    response = client.post(
        "/api/statements/import",
        files={"file": ("bank.csv", read_sample("bank_statement_dirty.csv"), "text/csv")},
        data={"side": "bank"},
    )
    assert response.status_code == 200
    data = postings(client)
    assert data["journals"] == [] and data["journal_count"] == 0
    store = client.app.state.store
    run_id = store.latest_run()["run_id"]
    assert store.posting_state(run_id)["scheme_version"] == POSTING_SCHEME_VERSION


def test_journal_ids_are_deterministic():
    # 凭证号纯由 match_id 派生，重跑/跨进程一致。
    assert posting.journal_id("m-abc") == posting.journal_id("m-abc")
    assert posting.journal_id("m-abc") != posting.journal_id("m-abd")
    assert posting.journal_id("m-abc").startswith("jrnl-")


def test_big_txn_no_kept_verbatim_in_posting_csv(client, demo):
    text = client.get("/api/export/postings.csv").text
    assert "9007199254740993" in text
    assert "e+" not in text.lower()


def test_clearing_subject_is_single_registered_code(client, demo):
    for line in lines(client):
        if line["line_type"] == LINE_VOUCHER:
            assert line["subject_code"] == CLEARING_SUBJECT[0]
