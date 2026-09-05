#!/usr/bin/env python3
"""Validity classifier for harness-feedback (nudge) trials.

A trial only counts as evidence if the agent actually reached the model and ran to a
natural settle. The Aug 25-26 batch produced nine `score: 0` records that looked like
results but were a DNS outage -- zero tokens, zero tool calls, `getaddrinfo ENOTFOUND
bedrock-runtime.us-east-1.amazonaws.com`. This module exists so infra failure can never
again be recorded as a model failure.

Statuses:
  VALID           reached the model AND settled naturally  -> counts as a trial
  VOID_NO_MODEL   never reached the model (0 tokens, 0 tool calls)
  VOID_NETWORK    network/DNS error and the run did not complete cleanly
  VOID_TIMEOUT    incomplete (timed out or never settled), no network evidence
  MISSING         no record on disk / unparseable

Only VALID is `done`. Everything else is retryable.

PATHS: the trajectory is located from the run's on-disk LAYOUT, not from the
absolute path stored in the record. run_task.py writes absolute paths at run time;
the tree moved once and every one of them dangled, which silently switched network
detection off -- a genuine VOID_NETWORK then classified as VOID_TIMEOUT, i.e. an
infra failure recorded as behaviour, which is exactly what this module exists to
prevent. See resolve_trajectory().
"""
import glob
import json
import os
import sys

NET_MARKERS = (
    "ENOTFOUND", "getaddrinfo", "EAI_AGAIN", "ECONNRESET", "ECONNREFUSED",
    "ETIMEDOUT", "pending stream has been canceled", "socket hang up",
    "Could not connect to the endpoint URL", "EPIPE",
)


def _total_tokens(rec):
    total = 0
    for rd in (rec.get("rounds") or []):
        usage = (rd or {}).get("usage") or {}
        try:
            total += int(usage.get("totalTokens") or 0)
        except (TypeError, ValueError):
            pass
    return total


def resolve_trajectory(stdout_path, rec):
    """Locate trajectory.jsonl for a record, independent of where the repo lives.

    Layout:  <runs_root>/<exp>.stdout.json
             <runs_root>/<exp>/_pi_meta/<instance>/trajectory.jsonl

    Tried in order: the stored path re-rooted onto this runs_root (exact artifact,
    stale prefix discarded), the canonical layout, an unambiguous glob, then the
    stored path verbatim for a tree that never moved. Returns None if none exist --
    callers must treat that as "unknown", never as "clean".
    """
    runs_root = os.path.dirname(os.path.abspath(stdout_path))
    base = os.path.basename(stdout_path)
    exp = base[:-len(".stdout.json")] if base.endswith(".stdout.json") else None
    stored = rec.get("trajectory")
    cands = []
    if stored and exp:
        parts = stored.replace("\\", "/").split("/")
        if exp in parts:
            i = len(parts) - 1 - parts[::-1].index(exp)
            cands.append(os.path.join(runs_root, *parts[i:]))
    if exp and rec.get("instance_id"):
        cands.append(os.path.join(runs_root, exp, "_pi_meta",
                                  rec["instance_id"], "trajectory.jsonl"))
    if exp:
        hits = sorted(glob.glob(os.path.join(runs_root, exp, "_pi_meta", "*",
                                             "trajectory.jsonl")))
        if len(hits) == 1:
            cands.append(hits[0])
    cands.append(stored)
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def _network_error(rec, path=None):
    """Scan the trajectory for transport-level failures (errorMessage fields only)."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", errors="replace") as fh:
            for line in fh:
                if "errorMessage" not in line:
                    continue
                for marker in NET_MARKERS:
                    if marker in line:
                        try:
                            ev = json.loads(line)
                        except Exception:
                            return marker
                        msg = ev.get("errorMessage") or ""
                        if not msg:
                            for v in ev.values():
                                if isinstance(v, dict) and v.get("errorMessage"):
                                    msg = v["errorMessage"]
                                    break
                        return (msg or marker)[:200]
    except OSError:
        return None
    return None


def classify(path, trajectory=None):
    """-> (status, detail_dict). See module docstring.

    `trajectory` lets a caller that already resolved the path pass it in; otherwise
    it is resolved here from the layout. Never read straight from the record.
    """
    if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
        return "MISSING", {}
    try:
        with open(path) as fh:
            rec = json.load(fh)
    except Exception as exc:
        return "MISSING", {"error": str(exc)}

    tokens = _total_tokens(rec)
    tools = int(rec.get("n_tool_calls_total") or 0)
    settled = bool(rec.get("settled"))
    timed_out = bool(rec.get("timed_out"))
    traj = trajectory or resolve_trajectory(path, rec)
    net = _network_error(rec, traj)
    detail = {
        "tokens": tokens, "tools": tools, "settled": settled,
        "timed_out": timed_out, "net_error": net, "score": rec.get("score"),
        "wall_s": rec.get("wall_clock_s"), "trajectory": traj,
    }
    if traj is None:
        # Not fatal here (token/settle fields alone can classify most runs), but the
        # caller must be able to see that the network check could not run.
        detail["trajectory_unresolved"] = True

    # Never reached the model at all.
    if tokens == 0 and tools == 0:
        return "VOID_NO_MODEL", detail
    # Incomplete AND transport failure -> infra, not behaviour.
    if net and (timed_out or not settled):
        return "VOID_NETWORK", detail
    # Incomplete for some other reason -> still not a clean trial.
    if timed_out or not settled:
        return "VOID_TIMEOUT", detail
    return "VALID", detail


def main():
    if len(sys.argv) < 2:
        sys.stderr.write("usage: nudge_validity.py <stdout.json> [...]\n")
        return 2
    worst = 0
    for path in sys.argv[1:]:
        status, detail = classify(path)
        label = os.path.basename(path).replace(".stdout.json", "")
        print(f"{label}\t{status}\t"
              f"tokens={detail.get('tokens')}\ttools={detail.get('tools')}\t"
              f"settled={detail.get('settled')}\ttimed_out={detail.get('timed_out')}\t"
              f"score={detail.get('score')}\tnet={detail.get('net_error')}"
              + ("\tTRAJECTORY-UNRESOLVED" if detail.get("trajectory_unresolved") else ""))
        if status != "VALID":
            worst = 1
    return worst


if __name__ == "__main__":
    sys.exit(main())
