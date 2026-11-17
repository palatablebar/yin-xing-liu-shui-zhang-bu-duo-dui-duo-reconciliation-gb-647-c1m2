"""完整入账方案：把匹配组生成正式记账凭证（每个匹配组一张凭证）。

设计目标（对应业务诉求）：
- 少量分录：同科目、同方向的来源对象合并成一条分录，一张凭证最多 5 条分录；
- 不被净额掩盖：每条分录保留被核销对象的全部原始流水号/凭证号，分录金额
  恒等于其引用对象金额之和，任何对象都不与其它对象轧差后消失；
- 借贷平衡：手续费单独成对（费用科目 ↔ 银行存款），组内容差尾差单独挂
  容差科目，平衡关系可逐条复算；
- 确定性：凭证号、行号、金额都由匹配组确定性派生，同批重跑逐行一致。

本模块是纯函数，不碰存储；页面、CSV 导出与落库都只调用这里，保证同一版方案。
"""
from __future__ import annotations

import hashlib
from decimal import Decimal

from ...config import (
    BANK_ACCOUNT_SUBJECTS,
    CLEARING_SUBJECT,
    FEE_EXPENSE_SUBJECT,
    GAP_LOSS_SUBJECT,
    POSTING_SCHEME_VERSION,
    dec,
)

SCHEME_VERSION = POSTING_SCHEME_VERSION
ZERO = Decimal("0.00")

# 分录类型：银行存款（银行流水）、待核销往来（账簿凭证）、
# 银行存款-手续费现金、手续费费用、容差尾差。
LINE_BANK = "bank"
LINE_VOUCHER = "voucher"
LINE_FEE_CASH = "fee_cash"
LINE_FEE_EXPENSE = "fee_expense"
LINE_GAP = "gap"

LINE_TYPE_LABELS = {
    LINE_BANK: "银行存款",
    LINE_VOUCHER: "待核销往来",
    LINE_FEE_CASH: "银行存款-手续费",
    LINE_FEE_EXPENSE: "手续费费用",
    LINE_GAP: "容差尾差",
}


def scheme_version() -> str:
    return SCHEME_VERSION


def journal_id(match_id: str) -> str:
    """凭证号由匹配组确定性派生，同批重跑不变。"""
    digest = hashlib.sha1(f"journal|{match_id}".encode("utf-8")).hexdigest()[:10]
    return f"jrnl-{digest}"


def line_id(journal: str, index: int) -> str:
    return f"{journal}-L{index:02d}"


def _index_records(records: list[dict]) -> dict[str, dict]:
    return {
        record["txn_no"]: record
        for record in records
        if record["status"] == "valid"
    }


def _signed_amount(record: dict) -> Decimal:
    return dec(record["amount"]) * int(record["direction"])


def _bank_subject(account: str) -> tuple[str, str]:
    return BANK_ACCOUNT_SUBJECTS.get(account, ("100299", f"银行存款-{account or '未知账户'}"))


def _make_line(index: int, journal: str, line_type: str, subject_code: str,
               subject_name: str, signed: Decimal, refs: list[str],
               ref_side: str, memo: str) -> dict:
    """构造一条分录。金额存借/贷两栏（非负），同时保留签向金额便于复算。

    入账签向与流水签向相反：银行资金流出（d=-1）在贷方核销，被核销凭证在
    借方；这里的 signed 是「分录签向」（借正贷负），不是原始流水签向。
    """
    debit = signed if signed > 0 else ZERO
    credit = -signed if signed < 0 else ZERO
    return {
        "line_no": index,
        "line_id": line_id(journal, index),
        "line_type": line_type,
        "subject_code": subject_code,
        "subject_name": subject_name,
        "debit": debit.quantize(Decimal("0.01")),
        "credit": credit.quantize(Decimal("0.01")),
        "signed": signed.quantize(Decimal("0.01")),
        "ref_side": ref_side,
        "refs": list(refs),
        "memo": memo,
    }


def build_journal(group: dict, record_index: dict[str, dict]) -> dict:
    """把一个匹配组展开成一张完整记账凭证。

    分录清单（同科目同方向合并，保留全部来源号）：
    1. 银行存款：按银行账户合并的银行流水分录（每条流水号都在 refs 里）；
    2. 待核销往来：账簿凭证分录（每张凭证号都在 refs 里）；
    3. 银行存款-手续费：按账户合并的手续费现金分录（固定贷方支出）；
    4. 手续费费用：费用科目分录（固定借方）；
    5. 容差尾差：银行与账簿签向金额不等时挂尾差科目，补齐借贷。
    """
    journal = journal_id(group["match_id"])
    bank_rows = [record_index[key] for key in group["bank_ids"] if key in record_index]
    ledger_rows = [record_index[key] for key in group["ledger_ids"] if key in record_index]
    if len(bank_rows) != len(group["bank_ids"]) or len(ledger_rows) != len(group["ledger_ids"]):
        raise ValueError(f"匹配组 {group['match_id']} 引用了不存在或已隔离的流水")

    raw_lines: list[dict] = []

    # 1. 银行存款：按账户合并，签向 = 各流水 金额×方向 之和，不做跨账户轧差。
    bank_by_account: dict[str, list[dict]] = {}
    for row in bank_rows:
        bank_by_account.setdefault(row.get("account", ""), []).append(row)
    for account in sorted(bank_by_account):
        rows = sorted(bank_by_account[account], key=lambda item: item["txn_no"])
        signed = sum((_signed_amount(row) for row in rows), ZERO)
        code, name = _bank_subject(account)
        raw_lines.append({
            "type": LINE_BANK, "code": code, "name": name, "signed": signed,
            "refs": [row["txn_no"] for row in rows], "side": "bank",
            "memo": f"银行流水核销 {len(rows)} 笔",
        })

    # 2. 待核销往来：账簿凭证合并成一条，保留每张被核销凭证号。
    # 分录签向与原始凭证签向相反（银行付款 d=-1 → 借记待核销往来核销负债）。
    ledger_sorted = sorted(ledger_rows, key=lambda item: item["txn_no"])
    voucher_signed_source = sum((_signed_amount(row) for row in ledger_sorted), ZERO)
    voucher_signed = -voucher_signed_source
    raw_lines.append({
        "type": LINE_VOUCHER, "code": CLEARING_SUBJECT[0], "name": CLEARING_SUBJECT[1],
        "signed": voucher_signed,
        "refs": [row["txn_no"] for row in ledger_sorted], "side": "ledger",
        "memo": f"核销账簿凭证 {len(ledger_sorted)} 张",
    })

    # 3/4. 手续费：实际手续费按银行账户出现金（贷），费用科目一次入费用（借）。
    fee_by_account: dict[str, Decimal] = {}
    fee_refs: dict[str, list[str]] = {}
    for row in bank_rows:
        fee = dec(row.get("fee", "0"))
        if fee <= 0:
            continue
        account = row.get("account", "")
        fee_by_account[account] = fee_by_account.get(account, ZERO) + fee
        fee_refs.setdefault(account, []).append(row["txn_no"])
    fee_total = sum(fee_by_account.values(), ZERO)
    for account in sorted(fee_by_account):
        code, name = _bank_subject(account)
        raw_lines.append({
            "type": LINE_FEE_CASH, "code": code, "name": name,
            "signed": -fee_by_account[account],
            "refs": sorted(fee_refs[account]), "side": "bank",
            "memo": "银行手续费现金支出",
        })
    if fee_total > 0:
        raw_lines.append({
            "type": LINE_FEE_EXPENSE, "code": FEE_EXPENSE_SUBJECT[0],
            "name": FEE_EXPENSE_SUBJECT[1], "signed": fee_total,
            "refs": list(group["bank_ids"]), "side": "bank",
            "memo": "本组银行手续费",
        })

    # 5. 容差尾差：银行签向 − 账簿签向（手续费已独立成对，不进尾差）。
    # 尾差分录取差额的反号，使全凭证分录签向之和为 0（借贷平衡）。
    bank_signed = sum((_signed_amount(row) for row in bank_rows), ZERO)
    gap_source = (bank_signed - voucher_signed_source).quantize(Decimal("0.01"))
    gap = -gap_source
    if gap != 0:
        raw_lines.append({
            "type": LINE_GAP, "code": GAP_LOSS_SUBJECT[0], "name": GAP_LOSS_SUBJECT[1],
            # gap > 0（银行付得多）→ 尾差费用在借方；gap < 0 → 贷方冲回。
            "signed": gap,
            "refs": list(group["bank_ids"]) + list(group["ledger_ids"]),
            "side": "both", "memo": "组内容差尾差",
        })

    # 确定性排序：类型顺序 → 科目码 → 引用号，保证重跑逐行一致。
    type_order = {
        LINE_BANK: 0, LINE_VOUCHER: 1, LINE_FEE_CASH: 2,
        LINE_FEE_EXPENSE: 3, LINE_GAP: 4,
    }
    raw_lines.sort(key=lambda item: (type_order[item["type"]], item["code"],
                                     ",".join(item["refs"])))
    lines = [
        _make_line(index + 1, journal, item["type"], item["code"], item["name"],
                   item["signed"], item["refs"], item["side"], item["memo"])
        for index, item in enumerate(raw_lines)
    ]

    debit_total = sum((line["debit"] for line in lines), ZERO)
    credit_total = sum((line["credit"] for line in lines), ZERO)
    return {
        "journal_id": journal,
        "match_id": group["match_id"],
        "tier": int(group["tier"]),
        "line_count": len(lines),
        "bank_refs": list(group["bank_ids"]),
        "ledger_refs": list(group["ledger_ids"]),
        "debit_total": debit_total,
        "credit_total": credit_total,
        "amount_total": debit_total,
        "posted_at": _posted_at(bank_rows),
        "scheme_version": SCHEME_VERSION,
        "lines": lines,
    }


def _posted_at(bank_rows: list[dict]) -> str:
    """凭证日期取组内最早一笔银行流水的入账时间（UTC 字符串原样保留）。"""
    stamps = [row.get("posted_at", "") for row in bank_rows if row.get("posted_at")]
    return min(stamps) if stamps else ""


def build_postings(matches: list[dict], records: list[dict]) -> list[dict]:
    """把全部匹配组展开成凭证清单，顺序与匹配组（层级、match_id）一致。"""
    record_index = _index_records(records)
    journals = [build_journal(group, record_index) for group in matches]
    journals.sort(key=lambda journal: (journal["tier"], journal["journal_id"]))
    return journals


def assert_balanced(journals: list[dict]) -> None:
    """复算每张凭证借贷平衡，不平直接失败（不允许悄悄轧差）。"""
    for journal in journals:
        if journal["debit_total"] != journal["credit_total"]:
            raise ValueError(
                f"凭证 {journal['journal_id']} 借贷不平："
                f"借 {journal['debit_total']} ≠ 贷 {journal['credit_total']}"
            )
        signed_sum = sum((dec(line["signed"]) for line in journal["lines"]), ZERO)
        if signed_sum != ZERO:
            raise ValueError(
                f"凭证 {journal['journal_id']} 分录签向之和非 0：{signed_sum}"
            )
