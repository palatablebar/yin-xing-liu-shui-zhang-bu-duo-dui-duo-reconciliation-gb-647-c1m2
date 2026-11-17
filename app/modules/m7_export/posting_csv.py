"""入账凭证 CSV：一张凭证一组行，分录逐条展开，来源号逐字保留。

页面 /postings 与本导出都读取 store 里同一份 journals，不允许出现两套方案。
大整数业务号按字符串输出，不做数值转换（TRAP-09）。
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from ..m5_matching.subset import TIER_NAMES
from ..m8_posting.posting import LINE_TYPE_LABELS

HEADER = [
    "凭证号", "匹配组", "层级", "行号", "分录类型", "科目编码", "科目名称",
    "借方金额", "贷方金额", "来源侧", "原始流水/凭证", "摘要", "方案版本",
]


def _money(value) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def postings_text(journals: list[dict], scheme_version: str) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(HEADER)
    for journal in journals:
        tier = TIER_NAMES.get(int(journal["tier"]), "")
        for line in journal["lines"]:
            writer.writerow([
                journal["journal_id"],
                journal["match_id"],
                tier,
                line["line_no"],
                LINE_TYPE_LABELS.get(line["line_type"], line["line_type"]),
                line["subject_code"],
                line["subject_name"],
                _money(Decimal(line["debit"])),
                _money(Decimal(line["credit"])),
                line.get("ref_side", ""),
                " ".join(line["refs"]),
                line.get("memo", ""),
                journal.get("scheme_version", scheme_version),
            ])
    return "﻿" + buffer.getvalue()
