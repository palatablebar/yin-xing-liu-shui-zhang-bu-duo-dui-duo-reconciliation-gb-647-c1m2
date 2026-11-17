"""不变量：守恒、不重叠、手续费守恒、幂等、隔离不参与合计。"""
from decimal import Decimal


def test_bank_conservation(client, demo):
    identities = {item["key"]: item for item in client.get("/api/differences").json()["identities"]}
    assert identities["bank_amount"]["balanced"] is True
    assert identities["ledger_amount"]["balanced"] is True
    assert identities["bank_count"]["balanced"] is True
    assert identities["ledger_count"]["balanced"] is True


def test_disjoint_assignment(client, demo):
    groups = client.get("/api/matches").json()["groups"]
    bank = [key for group in groups for key in group["bank_ids"]]
    ledger = [key for group in groups for key in group["ledger_ids"]]
    assert len(bank) == len(set(bank))
    assert len(ledger) == len(set(ledger))


def test_fee_conservation(client, demo):
    data = client.get("/api/matches").json()
    allocated = sum((Decimal(group["fee_alloc"]) for group in data["groups"]), Decimal("0.00"))
    actual = sum((Decimal(group["fee_actual"]) for group in data["groups"]), Decimal("0.00"))
    assert allocated == actual == Decimal("22.31")
    assert allocated == Decimal(demo["totals"]["bank_fee"])


def test_rerun_row_counts_stable(client, demo):
    before = client.get("/api/statements", params={"side": "bank"}).json()["count"]
    ledger_before = client.get("/api/statements", params={"side": "ledger"}).json()["count"]
    client.post("/api/demo/run")
    after = client.get("/api/statements", params={"side": "bank"}).json()["count"]
    ledger_after = client.get("/api/statements", params={"side": "ledger"}).json()["count"]
    assert before == after == 22
    assert ledger_before == ledger_after == 19


def test_quarantine_excluded_from_totals(client, demo):
    rows = client.get("/api/statements", params={"side": "bank"}).json()["rows"]
    gross = sum((Decimal(row["amount"]) * int(row["direction"]) for row in rows), Decimal("0.00"))
    assert gross == Decimal(demo["totals"]["bank_gross"])
    assert Decimal("999.00") not in {Decimal(row["amount"]) for row in rows}


def test_posting_journals_balanced_and_traceable(client, demo):
    # 入账不变量：每张凭证借贷平衡，且匹配组内每个来源号都能追到一条分录。
    data = client.get("/api/postings").json()
    referenced = {
        ref for journal in data["journals"] for line in journal["lines"]
        for ref in line["refs"]
    }
    groups = client.get("/api/matches").json()["groups"]
    for group in groups:
        for ref in group["bank_ids"] + group["ledger_ids"]:
            assert ref in referenced
    for journal in data["journals"]:
        assert Decimal(journal["debit_total"]) == Decimal(journal["credit_total"])


def test_posting_rerun_does_not_add_or_lose_lines(client, demo):
    # 入账幂等：同 run_id 重跑后凭证号、分录数与物理行数完全不变。
    before = client.get("/api/postings").json()
    client.post("/api/demo/run")
    client.post("/api/postings/recover")
    after = client.get("/api/postings").json()
    assert after["journal_count"] == before["journal_count"]
    assert after["line_count"] == before["line_count"]
    assert [j["journal_id"] for j in after["journals"]] == \
        [j["journal_id"] for j in before["journals"]]
