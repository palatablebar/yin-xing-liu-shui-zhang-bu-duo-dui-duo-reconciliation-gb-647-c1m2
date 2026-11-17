"""入账服务：从匹配组生成完整凭证、原子落库、查询与中断恢复。

落库与查询都走 store；凭证内容只由 m8_posting.posting 的纯函数生成，
页面与导出共用同一份结果，不存在两套入账方案。
"""
from __future__ import annotations

from decimal import Decimal

from ...errors import DomainError
from . import posting


def _build(store, run: dict, settings) -> list[dict]:
    records = store.records(run["run_id"])
    matches = store.matches(run["run_id"])
    journals = posting.build_postings(matches, records)
    posting.assert_balanced(journals)
    return journals


def payload(store, run: dict | None) -> dict:
    if not run:
        return {
            "run_id": None,
            "scheme_version": posting.scheme_version(),
            "stale": False,
            "journal_count": 0,
            "line_count": 0,
            "journals": [],
            "debit_total": "0.00",
            "credit_total": "0.00",
        }
    state = store.posting_state(run["run_id"])
    journals = store.journals(run["run_id"])
    debit = sum((Decimal(item["debit_total"]) for item in journals), Decimal("0.00"))
    credit = sum((Decimal(item["credit_total"]) for item in journals), Decimal("0.00"))
    line_count = sum(len(item["lines"]) for item in journals)
    stored_scheme = state["scheme_version"] if state else ""
    return {
        "run_id": run["run_id"],
        "scheme_version": posting.scheme_version(),
        "stored_scheme_version": stored_scheme,
        # 旧批次或中断后残留旧方案时标记过期，提示一键重建。
        "stale": bool(stored_scheme) and stored_scheme != posting.scheme_version(),
        "journal_count": len(journals),
        "line_count": line_count,
        "journals": journals,
        "debit_total": format(debit, "f"),
        "credit_total": format(credit, "f"),
    }


def post_journals(store, run: dict | None, settings) -> dict:
    """按完整方案正式入账；同 run_id 重跑先删后插，凭证号与分录逐行一致。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    journals = _build(store, run, settings)
    store.replace_postings(run["run_id"], journals, posting.scheme_version())
    return payload(store, store.get_run(run["run_id"]))


def rebuild_with_matches(store, run: dict, matches: list[dict], records: list[dict],
                         settings) -> list[dict]:
    """匹配重求解后在同一事务里重建入账，供 M5 调用。"""
    journals = posting.build_postings(matches, records)
    posting.assert_balanced(journals)
    store.replace_matches_and_postings(
        run["run_id"], matches, journals, posting.scheme_version()
    )
    return journals


def recover(store, run: dict | None, settings) -> dict:
    """中断/旧版恢复：按当前完整方案确定性重建，不新增、不丢失分录。"""
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    return post_journals(store, run, settings)
