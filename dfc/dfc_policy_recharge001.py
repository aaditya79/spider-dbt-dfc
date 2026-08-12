#!/usr/bin/env python
"""Standalone CLI for the recharge001 DFC checker (Phase 2 two-way gate).

The checker logic lives in spider_agent/agent/dfc_check.py (single source of truth,
also imported by the agent loop in Phase 3). This is a thin CLI wrapper.
"""
import argparse, json, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spider_agent.agent.dfc_check import check_recharge001_discounts

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("db", help="path to a produced (or gold) recharge.duckdb")
    a = ap.parse_args()
    print(json.dumps(check_recharge001_discounts(a.db), indent=2, default=str))
