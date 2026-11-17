"""运行清单导出：run_id、输入 sha256、规则版本、参数、计数、合计与入账方案。"""
from __future__ import annotations

import json
from decimal import Decimal

from ...manifest import build_manifest
from ..m8_posting.posting import LINE_TYPE_LABELS, scheme_version


def _journal_summary(journals: list[dict]) -> dict:
    line_types: dict[str, int] = {}
    ref_count = 0
    for journal in journals:
        for line in journal["lines"]:
            line_types[line["line_type"]] = line_types.get(line["line_type"], 0) + 1
            ref_count += len(line["refs"])
    return {
        "line_type_counts": {
            LINE_TYPE_LABELS.get(key, key): value
            for key, value in sorted(line_types.items())
        },
        "source_ref_count": ref_count,
    }


def manifest_text(run: dict, reconciliation: dict | None = None,
                  posting_state: dict | None = None,
                  journals: list[dict] | None = None) -> str:
    payload = build_manifest(run)
    if reconciliation:
        payload["totals"] = {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in reconciliation["totals"].items()
        }
        payload["counts"].update(reconciliation["counts"])
    # 入账方案版本与计数写入清单，导出文件可自证用的是哪一版完整方案。
    payload["posting"] = {
        "scheme_version": scheme_version(),
        "stored_scheme_version": posting_state["scheme_version"] if posting_state else "",
        "stale": bool(
            posting_state and posting_state["scheme_version"] != scheme_version()
        ),
        "journal_count": posting_state["journal_count"] if posting_state else 0,
        "line_count": posting_state["line_count"] if posting_state else 0,
    }
    if journals is not None:
        payload["posting"].update(_journal_summary(journals))
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
