"""只追加台账。

任何状态变化都落成一条不可变台账分录：决定、额度预留/释放、
补贴结算/追回、撤回、复核失败、设备事件、争议开闭。分录以哈希链
串联，verify_chain 可证明原决定从未被篡改；冲正从不删除旧分录，
而是追加反向分录并引用原分录编号。
"""

import hashlib
import json
import threading
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone


def _canonical(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    entry_id: str
    at: str
    type: str
    payload: dict
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "entry_id": self.entry_id,
            "at": self.at,
            "type": self.type,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


class Ledger:
    """线程安全的追加式分录本。"""

    GENESIS = "0" * 64

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: list[LedgerEntry] = []

    @staticmethod
    def _digest(seq: int, at: str, entry_type: str, payload: dict, prev_hash: str) -> str:
        body = _canonical(
            {"seq": seq, "at": at, "type": entry_type,
             "payload": payload, "prev_hash": prev_hash}
        )
        return hashlib.sha256(body).hexdigest()

    def append(self, entry_type: str, payload: dict) -> LedgerEntry:
        with self._lock:
            seq = len(self._entries) + 1
            prev_hash = self._entries[-1].entry_hash if self._entries else self.GENESIS
            at = payload.get("_at") or datetime.now(timezone.utc).isoformat()
            # 深拷贝后再入账：调用方之后对原对象的任何修改都不能改写分录，
            # 否则哈希链会在 verify_chain 时暴露篡改。
            payload = deepcopy({k: v for k, v in payload.items() if k != "_at"})
            entry_hash = self._digest(seq, at, entry_type, payload, prev_hash)
            entry = LedgerEntry(
                seq=seq, entry_id=uuid.uuid4().hex, at=at,
                type=entry_type, payload=payload,
                prev_hash=prev_hash, entry_hash=entry_hash,
            )
            self._entries.append(entry)
            return entry

    def all(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries)

    def verify_chain(self) -> dict:
        """重放哈希链并校验分录序号连续。"""
        with self._lock:
            prev_hash = self.GENESIS
            for index, entry in enumerate(self._entries, start=1):
                if entry.seq != index:
                    return {"ok": False, "broken_at": entry.seq, "reason": "序号不连续"}
                digest = self._digest(entry.seq, entry.at, entry.type, entry.payload, prev_hash)
                if digest != entry.entry_hash or entry.prev_hash != prev_hash:
                    return {"ok": False, "broken_at": entry.seq, "reason": "哈希链断裂，台账被篡改"}
                prev_hash = entry.entry_hash
            return {"ok": True, "entries": len(self._entries)}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
