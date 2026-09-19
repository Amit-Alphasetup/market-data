"""Reason codes — plan section 4.4, copied verbatim (identical in the JS client, D4).

level:     ok | info | warn | block
certified: whether a CERTIFIED run may contain it: 'yes' | 'no' | 'conditional'
           conditional APPROVAL_EVIDENCE_MISSING: only if every such exception has provenance 'migrated-v2-2026-09-11'
           conditional LISTING_DATE_UNVERIFIED:   only if the run window for that symbol starts on/after firstObservedDate
"""

# code -> (level, certified-run eligibility, meaning)
REASONS = {
    "OK": ("ok", "yes", "valid"),
    "NOT_YET_LISTED": ("info", "yes", "date before verified listing"),
    "DELISTED_BEFORE_WINDOW": ("info", "yes", "delisted before window"),
    "ISIN_MISSING": ("warn", "yes", "registry has no ISIN"),
    "SMALL_OHLC_DISCREPANCY": ("warn", "yes", "OHLC inconsistency <= 0.05%"),
    "APPROVAL_EVIDENCE_MISSING": ("warn", "conditional", "exception has no recorded evidence"),
    "LISTING_DATE_UNVERIFIED": ("warn", "conditional", "no verified listingDate and first bar after windowStart"),
    "NOT_IN_EOD2": ("block", "no", "EOD2 has no file"),
    "NOT_IN_EXPORT": ("block", "no", "not in this snapshot"),
    "EXPORT_FAILED": ("block", "no", "exporter error"),
    "IDENTITY_MISMATCH": ("block", "no", "ISIN/series/kind mismatch"),
    "AMBIGUOUS_SYMBOL": ("block", "no", ">1 candidate file"),
    "WRONG_EXCHANGE_OR_KIND": ("block", "no", "ETF/INDEX/EQ mismatch"),
    "HASH_MISMATCH": ("block", "no", "bytes != manifest sha256"),
    "SCHEMA_INVALID": ("block", "no", "violates section 4.5"),
    "INVALID_DATE": ("block", "no", "bad date"),
    "NONFINITE_VALUE": ("block", "no", "NaN/Infinity/string number"),
    "MISSING_REQUIRED_FIELD": ("block", "no", "required field null"),
    "OHLC_INCONSISTENT": ("block", "no", "inconsistency > 0.05%"),
    "MISSING_EXPECTED_SESSION": ("block", "no", "session missing inside coverage range"),
    "INSUFFICIENT_WARMUP": ("block", "no", "too few bars before report start"),
    "MISSING_EXECUTION_OPEN": ("block", "no", "execution-session OPEN absent"),
    "ADJUSTMENT_POLICY_UNKNOWN": ("block", "no", "adjustment unknown"),
    "CORPORATE_ACTION_UNRESOLVED": ("block", "no", "split suspected, not in registry"),
    "ANOMALOUS_MOVE_UNRESOLVED": ("block", "no", "|ret| >= 15% not approved for that date"),
    "DATASET_VERSION_CHANGED": ("block", "no", "snapshot changed mid-run"),
    "CALENDAR_UNVERIFIED": ("block", "no", "date beyond verifiedThrough / calendar hash bad"),
    "STALE_DATASET": ("block", "no", "latest session behind expected"),
    "SOURCE_UNAVAILABLE": ("block", "no", "unreachable"),
    "RUN_CONTEXT_MISSING": ("block", "no", "data call inside a run without ctx"),
    "SNAPSHOT_PROVENANCE_MISSING": ("block", "no", "supplied history not loader-owned"),
}


def level(code):
    return REASONS[code][0]


def is_block(code):
    return REASONS[code][0] == "block"


def is_warn(code):
    return REASONS[code][0] == "warn"


def certified_eligibility(code):
    return REASONS[code][1]


def reason(code, **detail):
    """{"code": ..., **detail} — the shape stored in manifest files[X].reasons."""
    if code not in REASONS:
        raise KeyError("unknown reason code: " + code)
    r = {"code": code}
    r.update({k: v for k, v in detail.items() if v is not None})
    return r


# thresholds shared with the clients
ANOMALY_THRESHOLD = 0.15          # |close-to-close ret| >= 15%
OHLC_WARN_TOLERANCE = 0.0005      # 0.05 %
WARMUP_DAYS = 400                 # calendar days of history kept before the window start
