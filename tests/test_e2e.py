"""端到端：样例 → 采信 → 质量 → 候选 → 匹配 → 差异 → 导出。"""
import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"


PAGES = ("/", "/quarantine", "/candidates", "/matches", "/postings", "/batches", "/export")


def test_full_pipeline_from_samples(client):
    response = client.post("/api/demo/run")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "matched"
    assert body["counts"]["matched_groups"] == 11
    identities = client.get("/api/differences").json()["identities"]
    assert all(item["balanced"] for item in identities)
    assert "合计" in client.get("/api/export/report.csv").text
    assert client.get("/api/matches").json()["count"] == 11


def test_all_six_pages_return_200(client, demo):
    for path in PAGES:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "ledgerbank" in response.text


def test_second_demo_run_same_run_id(client, demo):
    again = client.post("/api/demo/run").json()
    assert again["run_id"] == demo["run_id"]
    assert again["counts"] == demo["counts"]
    assert again["totals"] == demo["totals"]
    rows = client.get("/api/statements", params={"side": "bank"}).json()
    assert rows["count"] == 22


def test_bad_import_then_recover(client):
    bad = client.post("/api/statements/import",
                      data={"side": "bank", "sample": "missing.csv"})
    assert bad.status_code == 404
    good = client.post("/api/demo/run")
    assert good.status_code == 200
    assert good.json()["status"] == "matched"


def test_expected_totals_fixture(client, demo):
    expected = json.loads((FIXTURES / "expected_totals.json").read_text(encoding="utf-8"))
    assert demo["totals"] == expected["totals"]
    assert demo["counts"] == expected["counts"]


def test_expected_match_groups_fixture(client, demo):
    expected = json.loads((FIXTURES / "expected_matches.json").read_text(encoding="utf-8"))
    groups = client.get("/api/matches").json()["groups"]
    observed = [
        (group["match_id"], group["tier"], group["bank_ids"], group["ledger_ids"], group["diff"])
        for group in groups
    ]
    wanted = [
        (group["match_id"], group["tier"], group["bank_ids"], group["ledger_ids"], group["diff"])
        for group in expected["groups"]
    ]
    assert observed == wanted


def test_posting_flow_end_to_end(client, demo):
    # 完整入账：凭证与匹配组一一对应、借贷平衡、页面与导出同一版方案。
    data = client.get("/api/postings").json()
    assert data["journal_count"] == demo["counts"]["matched_groups"] == 11
    assert data["stale"] is False
    for journal in data["journals"]:
        assert journal["debit_total"] == journal["credit_total"]
    # 页面与 CSV 导出的凭证号集合一致。
    csv_text = client.get("/api/export/postings.csv").text
    page_ids = {journal["journal_id"] for journal in data["journals"]}
    csv_ids = {line.split(",", 1)[0] for line in csv_text.lstrip("﻿").splitlines()[1:]}
    assert csv_ids == page_ids
    manifest = client.get("/api/export/manifest.json").json()
    assert manifest["posting"]["scheme_version"] == data["scheme_version"]
    assert manifest["posting"]["line_count"] == data["line_count"]
