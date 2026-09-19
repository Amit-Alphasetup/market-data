"""Hashes — plan section 4.6. One byte-level rule: every sha256 is over the exact bytes written and served."""
import hashlib
import json

CONTRACT_ID = "alphadesk-eod2/3"


def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj):
    """json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def policy_hash(adjustment_policy, splits, genuine_moves, quarantine, gap_policy):
    return sha256_bytes(canonical_json({"adjustmentPolicy": adjustment_policy, "splits": splits, "genuineMoves": genuine_moves,
                                        "quarantine": quarantine, "gapPolicy": gap_policy}))


def dataset_hash(files, calendar_sha256, policy_hash_hex):
    """files: {key: {"sha256": ...}}"""
    lines = sorted("file:%s:%s" % (k, v["sha256"]) for k, v in files.items())
    lines += ["calendar:%s" % calendar_sha256, "policy:%s" % policy_hash_hex, "contract:" + CONTRACT_ID]
    return sha256_bytes(("\n".join(lines) + "\n").encode("utf-8"))
