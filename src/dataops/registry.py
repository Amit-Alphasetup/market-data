"""Registry access helpers used by the exporter (config/universe.json, validated by schema_universe)."""
import json

from . import schema_universe as su


# Stocks (v4): EOD2's stock prices are split/bonus adjusted only from about 2007 (measured 2026-11-13: prices across known 2009-2024 splits and
# bonuses are continuous; before 2007 there are hundreds of unadjusted -50%..-90% overnight gaps). Nothing older is published for stocks.
EQ_HISTORY_FLOOR = "2007-01-01"


class Registry:
    def __init__(self, data):
        su.assert_valid(data)
        self.data = data
        self.default_start = data["defaultHistoryStart"]
        self.cutoff_ist = data["calendar"]["cutoffIST"]
        self.etfs = [x for x in data["instruments"] if x["enabled"] and x.get("kind", "ETF") != "EQ"]   # v3 publishes ETFs + indices only
        self.stocks = [x for x in data["instruments"] if x["enabled"] and x.get("kind") == "EQ"]       # v4 only (export_v4)
        self.eq_floor = data.get("eqHistoryFloor", EQ_HISTORY_FLOOR)
        self.indices = [x for x in data["indices"] if x["enabled"]]
        self.quarantine = data["quarantine"]
        self.gap_policy = data["gapPolicy"]
        self._splits, self._moves, self._bad, self._actions = {}, {}, {}, {}
        for a in data.get("actions", []):
            self._actions.setdefault(a["symbol"], []).append(a)
        for s in data["splits"]:
            self._splits.setdefault(s["symbol"], []).append(s)
        for m in data["genuineMoves"]:
            self._moves.setdefault(m["symbol"], []).append(m)
        for b in data.get("badPrints", []):
            self._bad.setdefault(b["symbol"], []).append(b)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    def splits_for(self, sym):
        return sorted(self._splits.get(sym, []), key=lambda s: s["date"])

    def approvals_on(self, sym):
        """{date: entry} of splits and genuineMoves for sym (per-date; a split entry also covers its own date)."""
        out = {}
        for e in self._moves.get(sym, []):
            out[e["date"]] = e
        for e in self._splits.get(sym, []):
            out.setdefault(e["date"], e)
        for e in self._actions.get(sym, []):
            out.setdefault(e["date"], e)
        return out

    def actions_for(self, sym):
        return sorted(self._actions.get(sym, []), key=lambda a: a["date"])

    def bad_prints_on(self, sym):
        """Dates flagged as not-real prints for sym — dropped from its bars before the anomaly scan runs,
        so the exact same MISSING_EXPECTED_SESSION handling that already covers a genuinely absent
        session covers a flagged one too. No new reason code, no new engine-facing behaviour."""
        return {e["date"] for e in self._bad.get(sym, [])}

    def quarantine_reason(self, sym):
        q = self.quarantine
        if isinstance(q, dict):
            return q.get(sym)
        for e in q:
            if isinstance(e, dict) and e.get("symbol") == sym:
                return e.get("reason", "quarantined")
        return None

    def known_no_trade(self, sym):
        return set((self.gap_policy.get("known_no_trade") or {}).get(sym, []))

    def gap_allow_all(self, sym):
        return (self.gap_policy.get("gap_allow_all") or {}).get(sym)

    def window_start(self, inst):
        """Hard lower bound of the instrument's window: max(defaultHistoryStart, historyStart)."""
        hs = inst.get("historyStart")
        return max(self.default_start, hs) if hs else self.default_start

    @property
    def adjustment_policy(self):
        return "eod2-split-adjusted-price-only"
