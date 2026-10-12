"""Event inbox: events/inbox.jsonl (append-only, daemon-written) + events/acks.json."""

from __future__ import annotations

import threading
import time

from . import config, store


class Inbox:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.events: list[dict] = store.read_jsonl(config.events_path())
        self.seq = max((e.get("seq", 0) for e in self.events), default=0)
        self.acked: set[int] = set(store.read_json(self._acks_path(), {"acked": []})["acked"])

    @staticmethod
    def _acks_path():
        return config.events_path().parent / "acks.json"

    def emit(self, type_: str, **fields) -> dict:
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "time": store.now_iso(), "ts": time.time(), "type": type_, **fields}
            store.append_jsonl(config.events_path(), ev)
            self.events.append(ev)
            self.cond.notify_all()
            return ev

    def query(self, since: int = 0, unacked: bool = True, types: list[str] | None = None,
              node_prefix: str | None = None, limit: int = 100) -> list[dict]:
        out = []
        for ev in self.events:
            if ev["seq"] <= since:
                continue
            if unacked and ev["seq"] in self.acked:
                continue
            if types and ev["type"] not in types:
                continue
            if node_prefix and not str(ev.get("node", "")).startswith(node_prefix):
                continue
            out.append(dict(ev, acked=ev["seq"] in self.acked))
        return out[-limit:]

    def wait(self, timeout: float, **query) -> list[dict]:
        deadline = time.time() + timeout
        with self.cond:
            while True:
                found = self.query(**query)
                left = deadline - time.time()
                if found or left <= 0:
                    return found
                self.cond.wait(min(left, 5.0))

    def ack(self, seqs: list[int] | None = None, upto: int | None = None) -> int:
        with self.cond:
            before = len(self.acked)
            if seqs:
                self.acked.update(int(s) for s in seqs)
            if upto is not None:
                self.acked.update(e["seq"] for e in self.events if e["seq"] <= upto)
            store.write_json(self._acks_path(), {"acked": sorted(self.acked)})
            return len(self.acked) - before
