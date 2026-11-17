"""导出服务：报告 CSV、入账凭证 CSV 与运行清单 JSON。"""
from __future__ import annotations

from ...errors import DomainError
from ..m6_diff import differences
from ..m8_posting import posting as posting_mod
from . import csv_out, manifest_out, posting_csv


def _reconciliation(store, run: dict, settings) -> dict:
    return differences.reconcile(store.records(run["run_id"]), store.matches(run["run_id"]), settings)


def report(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _reconciliation(store, run, settings)
    return csv_out.report_text(store.records(run["run_id"]), store.matches(run["run_id"]), result)


def postings_report(store, run: dict | None) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    # 与 /postings 页面同读一张 journals 表，页面和导出天然同一版方案。
    journals = store.journals(run["run_id"])
    return posting_csv.postings_text(journals, posting_mod.scheme_version())


def manifest(store, run: dict | None, settings) -> str:
    if not run:
        raise DomainError("E_NO_RUN", "还没有批次", 404)
    result = _reconciliation(store, run, settings)
    state = store.posting_state(run["run_id"])
    journals = store.journals(run["run_id"])
    return manifest_out.manifest_text(run, result, state, journals)


def summary(store, run: dict | None, settings) -> dict:
    if not run:
        return {"run_id": None, "counts": {}, "totals": {}}
    result = _reconciliation(store, run, settings)
    state = store.posting_state(run["run_id"])
    return {
        "run_id": run["run_id"],
        "counts": result["counts"],
        "totals": {key: format(value, "f") for key, value in result["totals"].items()},
        "posting": {
            "scheme_version": posting_mod.scheme_version(),
            "stored_scheme_version": state["scheme_version"] if state else "",
            "stale": bool(state and state["scheme_version"] != posting_mod.scheme_version()),
            "journal_count": state["journal_count"] if state else 0,
            "line_count": state["line_count"] if state else 0,
        },
    }
