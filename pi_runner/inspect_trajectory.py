#!/usr/bin/env python
"""Pull the interesting steps out of one Pi trajectory.

Usage:
  inspect_trajectory.py <trajectory.jsonl> [--dbt] [--edits] [--errors] [--tail N] [--all]

Default shows a compact step list (tool calls + their result heads).
"""
import argparse
import json


def load(path):
    evs = []
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                evs.append(json.loads(raw.decode("utf-8")))
            except Exception:
                pass
    return evs


def result_text(e):
    """Extract text from a tool_execution_end event."""
    r = e.get("result") or {}
    if isinstance(r, str):
        return r
    out = r.get("output") or r.get("content") or r.get("text")
    if isinstance(out, list):
        parts = []
        for c in out:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts)
    if isinstance(out, str):
        return out
    return json.dumps(r)[:2000]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--dbt", action="store_true", help="only dbt bash calls + results")
    ap.add_argument("--edits", action="store_true", help="only edit/write calls")
    ap.add_argument("--errors", action="store_true", help="only results mentioning errors")
    ap.add_argument("--tail", type=int, default=0, help="last N steps only")
    ap.add_argument("--width", type=int, default=1200)
    args = ap.parse_args()

    evs = load(args.path)
    steps = []
    pending = {}
    for e in evs:
        t = e.get("type")
        if t == "tool_execution_start":
            pending[e.get("toolCallId")] = {
                "tool": e.get("toolName"), "args": e.get("args") or {}}
        elif t == "tool_execution_end":
            cid = e.get("toolCallId")
            s = pending.pop(cid, {"tool": "?", "args": {}})
            s["result"] = result_text(e)
            s["isError"] = bool(e.get("isError"))
            steps.append(s)

    sel = []
    for i, s in enumerate(steps, 1):
        cmd = str(s["args"].get("command", ""))
        if args.dbt and not (s["tool"] == "bash" and "dbt" in cmd):
            continue
        if args.edits and s["tool"] not in ("edit", "write"):
            continue
        if args.errors and not (
            s["isError"] or any(k in (s.get("result") or "").lower()
                                for k in ("error", "fail", "traceback", "compilation"))):
            continue
        sel.append((i, s))

    if args.tail:
        sel = sel[-args.tail:]

    for i, s in sel:
        a = s["args"]
        label = a.get("command") or a.get("path") or a.get("pattern") or json.dumps(a)[:120]
        print(f"\n{'='*90}\nSTEP {i}  [{s['tool']}]  isError={s['isError']}\n{'-'*90}")
        print(f"$ {str(label)[:600]}")
        print(f"{'-'*90}")
        print((s.get("result") or "")[:args.width])
    print(f"\n({len(sel)} of {len(steps)} steps shown)")


if __name__ == "__main__":
    main()
