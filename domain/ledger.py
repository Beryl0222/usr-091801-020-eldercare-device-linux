"""家庭年度补贴额度台账。

台账只追加、不修改：占用 ``OCCUPY``、释放预留 ``RELEASE``、
结算 ``SETTLE``、冲正 ``REVERSE`` 各自独立成条；余额由全量分录求和重放得到，
任何更正都以反向分录完成，原决定与原分录永远保留。

所有写操作在同一把锁内"先校验、后落账"，批量占用要么全部成功要么全部失败，
因此并发提交的申请不可能把同一年度额度超占。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import count
from threading import RLock

from .errors import QuotaExceeded
from .money import utc_now

OCCUPY = "OCCUPY"
RELEASE = "RELEASE"
SETTLE = "SETTLE"
REVERSE = "REVERSE"

_SIGNS = {OCCUPY: 1, RELEASE: -1, SETTLE: 1, REVERSE: -1}


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    household_id: str
    year: int
    kind: str
    amount_cents: int
    application_id: str
    reason: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "household_id": self.household_id,
            "year": self.year,
            "kind": self.kind,
            "amount_cents": self.amount_cents,
            "application_id": self.application_id,
            "reason": self.reason,
            "created_at": self.created_at,
        }


class QuotaLedger:
    def __init__(self):
        self._lock = RLock()
        self._entries = []
        self._seq = count(1)

    # ------------------------------------------------------------------ #
    # 读侧
    # ------------------------------------------------------------------ #
    def _replay(self):
        """重放全部分录，返回 committed[household, year] 与按申请的预留。"""
        committed = {}
        reserved = {}
        for e in self._entries:
            key = (e.household_id, e.year)
            amount = _SIGNS[e.kind] * e.amount_cents
            committed[key] = committed.get(key, 0) + amount
            rkey = (e.application_id, e.household_id, e.year)
            if e.kind == OCCUPY:
                reserved[rkey] = reserved.get(rkey, 0) + e.amount_cents
            elif e.kind == RELEASE:
                reserved[rkey] = reserved.get(rkey, 0) - e.amount_cents
        return committed, reserved

    def balances(self, household_id, year):
        with self._lock:
            reserved = settled = 0
            for e in self._entries:
                if e.household_id == household_id and e.year == year:
                    if e.kind == OCCUPY:
                        reserved += e.amount_cents
                    elif e.kind == RELEASE:
                        reserved -= e.amount_cents
                    elif e.kind == SETTLE:
                        settled += e.amount_cents
                    elif e.kind == REVERSE:
                        settled -= e.amount_cents
            return reserved, settled

    def proof(self, household_id, year, annual_cap_cents, pending=0):
        """生成额度证明；pending 为待落账的本批占用合计（供拒绝时展示）。"""
        reserved, settled = self.balances(household_id, year)
        committed = reserved + settled
        return {
            "household_id": household_id,
            "year": year,
            "annual_cap_cents": annual_cap_cents,
            "reserved_cents": reserved,
            "settled_cents": settled,
            "committed_cents": committed,
            "pending_cents": pending,
            "available_cents": annual_cap_cents - committed,
            "would_remain_cents": annual_cap_cents - committed - pending,
            "over_occupy": committed + pending > annual_cap_cents,
            "entry_count": sum(
                1
                for e in self._entries
                if e.household_id == household_id and e.year == year
            ),
            "as_of": utc_now().isoformat(),
        }

    def entries(self, household_id=None, application_id=None):
        with self._lock:
            out = self._entries
            if household_id is not None:
                out = [e for e in out if e.household_id == household_id]
            if application_id is not None:
                out = [e for e in out if e.application_id == application_id]
            return [e.to_dict() for e in out]

    # ------------------------------------------------------------------ #
    # 写侧（全部在锁内先校验后落账）
    # ------------------------------------------------------------------ #
    def _append(self, household_id, year, kind, amount_cents, application_id, reason):
        if amount_cents < 0:
            raise ValueError("台账分录金额必须为非负整数，方向由 kind 决定")
        entry = LedgerEntry(
            seq=next(self._seq),
            household_id=household_id,
            year=year,
            kind=kind,
            amount_cents=amount_cents,
            application_id=application_id,
            reason=reason,
            created_at=utc_now().isoformat(),
        )
        self._entries.append(entry)
        return entry

    def occupy_batch(self, reservations, caps):
        """原子批量占用。

        reservations: [(household_id, year, amount_cents, application_id, reason)]
        caps:         {(household_id, year): annual_cap_cents}
        任一年度额度不足即整批拒绝，不产生任何分录。
        """
        with self._lock:
            committed, _ = self._replay()
            pending = {}
            for household_id, year, amount, _app, _reason in reservations:
                key = (household_id, year)
                pending[key] = pending.get(key, 0) + amount
            for key, pending_amount in pending.items():
                cap = caps[key]
                used = committed.get(key, 0)
                if used + pending_amount > cap:
                    raise QuotaExceeded(
                        f"{key[0]} {key[1]} 年度补贴额度不足",
                        self.proof(key[0], key[1], cap, pending_amount),
                    )
            created = []
            for household_id, year, amount, app, reason in reservations:
                created.append(
                    self._append(household_id, year, OCCUPY, amount, app, reason)
                )
            return [e.to_dict() for e in created]

    def release_reservation(self, application_id, household_id, year, amount, reason):
        with self._lock:
            _, reserved = self._replay()
            key = (application_id, household_id, year)
            if amount > reserved.get(key, 0):
                raise ValueError(
                    f"释放金额 {amount} 超过申请 {application_id} 的预留 "
                    f"{reserved.get(key, 0)}"
                )
            return self._append(
                household_id, year, RELEASE, amount, application_id, reason
            ).to_dict()

    def settle_segment(self, application_id, household_id, year, reserved_amount,
                       actual_cents, annual_cap_cents, reason):
        """分段结算：先释放该段预留，再按实际金额落 SETTLE。

        结算同样不得越过年度额度（实际一般不大于预留）。
        """
        with self._lock:
            committed, reserved = self._replay()
            if reserved_amount:
                rkey = (application_id, household_id, year)
                if reserved_amount > reserved.get(rkey, 0):
                    raise ValueError("结算释放金额超过该申请预留")
            cap_key = (household_id, year)
            projected = committed.get(cap_key, 0) - reserved_amount + actual_cents
            if projected > annual_cap_cents:
                raise QuotaExceeded(
                    "结算后将超过年度额度",
                    self.proof(household_id, year, annual_cap_cents, actual_cents),
                )
            entries = []
            if reserved_amount:
                entries.append(
                    self._append(
                        household_id, year, RELEASE, reserved_amount,
                        application_id, f"释放预留：{reason}",
                    )
                )
            entries.append(
                self._append(
                    household_id, year, SETTLE, actual_cents,
                    application_id, reason,
                )
            )
            return [e.to_dict() for e in entries]

    def reverse(self, application_id, household_id, year, amount_cents, reason):
        """冲正已结算金额（追回/退差），只追加 REVERSE 分录，不改原结算。"""
        with self._lock:
            if amount_cents <= 0:
                raise ValueError("冲正金额必须为正")
            return self._append(
                household_id, year, REVERSE, amount_cents, application_id, reason
            ).to_dict()
