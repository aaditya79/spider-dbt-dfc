#!/usr/bin/env python
"""Regression checks for the CreateFile/EditFile truncation fix.

(i)   a COMPLETE large multi-line SQL body parses -> correct CreateFile action.
(ii)  the REAL truncated response (opening ``` but no closing) -> parser returns None
      AND is_truncated_file_action() flags it (so the loop gives the specific hint).
(iii) a malformed-but-not-truncated response -> still rejected (None), and NOT
      misclassified as truncation (so acceptance was not loosened, no partial write).
"""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from spider_agent.agent.action import CreateFile, EditFile
from spider_agent.agent.agents import PromptAgent

trunc = json.load(open("output/claude-opus-4-8-ecom-e2-s50/recharge002/spider/result.json"))["trajectory"][15]["response"]
complete = trunc + "\n```"   # same body, now with a closing fence (what 8192 tokens allows)

fails = 0

# (i) complete large body parses
a = CreateFile.parse_action_from_text(complete)
ok_i = (a is not None and a.filepath == "models/recharge__customer_daily_rollup.sql"
        and len(a.code) > 5000 and "{{ ref(" in a.code
        and not PromptAgent.is_truncated_file_action(complete))
print(f"(i)   complete large body -> {'PASS' if ok_i else 'FAIL'}"
      + (f"  [filepath={a.filepath}, code={len(a.code)} chars]" if a else "  [no action]"))
fails += not ok_i

# (ii) truncated -> rejected + flagged
a2 = CreateFile.parse_action_from_text(trunc)
e2 = EditFile.parse_action_from_text(trunc)
flagged = PromptAgent.is_truncated_file_action(trunc)
ok_ii = (a2 is None and e2 is None and flagged is True)
print(f"(ii)  truncated body    -> {'PASS' if ok_ii else 'FAIL'}"
      f"  [CreateFile={a2}, EditFile={e2}, is_truncated_file_action={flagged} -> file NOT written]")
fails += not ok_ii

# (iii) malformed, not truncated -> rejected, not misclassified
cases = {
    "CreateFile, no fence at all": 'Thought: x\nAction: CreateFile(filepath="models/foo.sql")\nselect 1',
    "unknown garbage action":      'Thought: x\nAction: DoSomething(weird payload) with no fence',
}
ok_iii = True
for name, resp in cases.items():
    parsed = CreateFile.parse_action_from_text(resp) or EditFile.parse_action_from_text(resp)
    mis = PromptAgent.is_truncated_file_action(resp)
    good = (parsed is None and mis is False)   # rejected AND not called truncation
    ok_iii &= good
    print(f"(iii) {name:<28}-> {'PASS' if good else 'FAIL'}  [parsed={parsed}, flagged_truncation={mis}]")
fails += not ok_iii

print("\nALL REGRESSION CHECKS:", "PASS" if fails == 0 else f"FAIL ({fails} group(s) failed)")
sys.exit(1 if fails else 0)
