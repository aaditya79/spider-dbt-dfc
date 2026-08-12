import base64
import json
import logging
import os
import re
import time
import uuid
from http import HTTPStatus
from io import BytesIO
from typing import Dict, List
from spider_agent.agent.prompts import BIGQUERY_SYSTEM, LOCAL_SYSTEM, DBT_SYSTEM, SNOWFLAKE_SYSTEM, CH_SYSTEM, PG_SYSTEM,REFERENCE_PLAN_SYSTEM
from spider_agent.agent.action import Action, Bash, Terminate, CreateFile, EditFile, LOCAL_DB_SQL, BIGQUERY_EXEC_SQL, SNOWFLAKE_EXEC_SQL, BQ_GET_TABLES, BQ_GET_TABLE_INFO, BQ_SAMPLE_ROWS, SF_GET_TABLES, SF_GET_TABLE_INFO, SF_SAMPLE_ROWS
from spider_agent.envs.spider_agent import Spider_Agent_Env
from spider_agent.agent.models import call_llm


from openai import AzureOpenAI
from typing import Dict, List, Optional, Tuple, Any, TypedDict




logger = logging.getLogger("spider_agent")


class PromptAgent:
    def __init__(
        self,
        model="gpt-4",
        max_tokens=1500,
        top_p=0.9,
        temperature=0.5,
        max_memory_length=10,
        max_steps=15,
        use_plan=False,
        dfc_policy=None
    ):

        self.model = model
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.temperature = temperature
        self.max_memory_length = max_memory_length
        self.max_steps = max_steps
        
        self.thoughts = []
        self.responses = []
        self.actions = []
        self.observations = []
        self.system_message = ""
        self.history_messages = []
        self.env = None
        self.codes = []
        self.work_dir = "/workspace"
        self.use_plan = use_plan

        # --- DFC scoped policy (recharge001 only) -------------------------------
        # A post-materialization checker that STEERS the agent (injects a
        # violation-specific retry observation). It never decides the final score.
        self.dfc_policy = dfc_policy          # e.g. "recharge001" or None
        self.dfc_retries = 0
        self.dfc_max_retries = 3
        self.dfc_events = []                  # recorded violation rounds, for the audit sidecar

    def set_env_and_task(self, env: Spider_Agent_Env):
        self.env = env
        self.thoughts = []
        self.responses = []
        self.actions = []
        self.observations = []
        self.codes = []
        self.history_messages = []
        self.instruction = self.env.task_config['instruction']
        if 'plan' in self.env.task_config:
            self.reference_plan = self.env.task_config['plan']
        
        
        if self.env.task_config['type'] == 'Bigquery':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, BIGQUERY_EXEC_SQL, BQ_GET_TABLES, BQ_GET_TABLE_INFO, BQ_SAMPLE_ROWS, CreateFile, EditFile]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = BIGQUERY_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        elif self.env.task_config['type'] == 'Snowflake':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, SNOWFLAKE_EXEC_SQL, SF_GET_TABLES, SF_GET_TABLE_INFO, SF_SAMPLE_ROWS, CreateFile, EditFile]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = SNOWFLAKE_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        elif self.env.task_config['type'] == 'Local':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, CreateFile, EditFile, LOCAL_DB_SQL]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = LOCAL_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        elif self.env.task_config['type'] == 'DBT':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, CreateFile, EditFile, LOCAL_DB_SQL]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = DBT_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        elif self.env.task_config['type'] == 'Postgres':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, CreateFile, EditFile]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = PG_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        elif self.env.task_config['type'] == 'Clickhouse':
            self._AVAILABLE_ACTION_CLASSES = [Bash, Terminate, CreateFile, EditFile]
            action_space = "".join([action_cls.get_action_description() for action_cls in self._AVAILABLE_ACTION_CLASSES])
            self.system_message = CH_SYSTEM.format(work_dir=self.work_dir, action_space=action_space, task=self.instruction, max_steps=self.max_steps)
        
        if self.use_plan:
            self.system_message += REFERENCE_PLAN_SYSTEM.format(plan=self.reference_plan)
        


        self.history_messages.append({
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": self.system_message 
                },
            ]
        })
        
    def predict(self, obs: Dict=None) -> List:
        """
        Predict the next action(s) based on the current observation.
        """    
        
        assert len(self.observations) == len(self.actions) and len(self.actions) == len(self.thoughts) \
            , "The number of observations and actions should be the same."

        status = False
        while not status:
            messages = self.history_messages.copy()
            messages.append({
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Observation: {}\n".format(str(obs))
                    }
                ]
            })  
            status, response = call_llm({
                "model": self.model,
                "messages": messages,
                "max_tokens": self.max_tokens,
                "top_p": self.top_p,
                "temperature": self.temperature
            })
            response = response.strip()
            if not status:
                if response in ["context_length_exceeded","rate_limit_exceeded","max_tokens","unknown_error"]:
                    self.history_messages = [self.history_messages[0]] + self.history_messages[3:]
                else:
                    raise Exception(f"Failed to call LLM, response: {response}")
            

        try:
            action = self.parse_action(response)
            thought = re.search(r'Thought:(.*?)Action', response, flags=re.DOTALL)
            if thought:
                thought = thought.group(1).strip()
            else:
                thought = response
        except ValueError as e:
            print("Failed to parse action from response", e)
            action = None
        
        logger.info("Observation: %s", obs)
        logger.info("Response: %s", response)

        self._add_message(obs, thought, action)
        self.observations.append(obs)
        self.thoughts.append(thought)
        self.responses.append(response)
        self.actions.append(action)

        # if action is not None:
        #     self.codes.append(action.code)
        # else:
        #     self.codes.append(None)

        return response, action
        
    
    def _add_message(self, observations: str, thought: str, action: Action):
        self.history_messages.append({
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Observation: {}".format(observations)
                }
            ]
        })
        self.history_messages.append({
            "role": "assistant",
            "content": [
                {
                    "type": "text",
                    "text": "Thought: {}\n\nAction: {}".format(thought, str(action))
                }
            ]
        })
        if len(self.history_messages) > self.max_memory_length*2+1:
            self.history_messages = [self.history_messages[0]] + self.history_messages[-self.max_memory_length*2:]
    
    @staticmethod
    def is_truncated_file_action(response: str) -> bool:
        """True iff the response is a CreateFile/EditFile whose ``` fence was OPENED but
        never CLOSED — i.e. the file body was cut off (almost always by the output-token
        limit). We use this ONLY to give the model a specific retry hint; we NEVER accept or
        write the partial content (CreateFile/EditFile.parse_action_from_text still requires a
        closing fence and returns None here, so nothing is written).
        """
        if response is None:
            return False
        # an opening fence exists for a file action ...
        opened = re.search(r'(?:CreateFile|EditFile)\(filepath=.*?\).*?```', response, flags=re.DOTALL)
        if not opened:
            return False
        # ... but no COMPLETE (closed) file action parses out of it.
        from spider_agent.agent.action import CreateFile, EditFile
        return (CreateFile.parse_action_from_text(response) is None
                and EditFile.parse_action_from_text(response) is None)

    def parse_action(self, output: str) -> Action:
        """ Parse action from text """
        if output is None or len(output) == 0:
            pass
        action_string = ""
        patterns = [r'["\']?Action["\']?:? (.*?)Observation',r'["\']?Action["\']?:? (.*?)Thought', r'["\']?Action["\']?:? (.*?)$', r'^(.*?)Observation']

        for p in patterns:
            match = re.search(p, output, flags=re.DOTALL)
            if match:
                action_string = match.group(1).strip()
                break
        if action_string == "":
            action_string = output.strip()
        
        output_action = None
        for action_cls in self._AVAILABLE_ACTION_CLASSES:
            action = action_cls.parse_action_from_text(action_string)
            if action is not None:
                output_action = action
                break
        if output_action is None:
            action_string = action_string.replace("\_", "_").replace("'''","```")
            for action_cls in self._AVAILABLE_ACTION_CLASSES:
                action = action_cls.parse_action_from_text(action_string)
                if action is not None:
                    output_action = action
                    break
        
        return output_action
    

    
    def run(self):
        assert self.env is not None, "Environment is not set."
        result = ""
        done = False
        step_idx = 0
        obs = "You are in the folder now."
        retry_count = 0
        last_action = None
        repeat_action = False
        while not done and step_idx < self.max_steps:

            response, action = self.predict(
                obs
            )
            if action is None:
                retry_count += 1
                if retry_count > 3:
                    logger.info("Failed to parse action from response, stop.")
                    break
                if self.is_truncated_file_action(response):
                    # Opening ``` but no closing ``` -> body was truncated (output-token limit).
                    # The file was NOT written. Tell the model specifically so it can shorten/split
                    # instead of re-emitting the same oversized action and dying on repeat.
                    logger.info("Failed to parse action: truncated/unclosed file body, try again.")
                    obs = ("Your CreateFile/EditFile content was not closed with a matching ``` fence "
                           "- it was likely truncated for exceeding the output length limit, and the "
                           "file was NOT written. Write a shorter file body, or build it incrementally "
                           "with multiple EditFile edits, and be sure to end with a closing ```.")
                else:
                    logger.info("Failed to parse action from response, try again.")
                    obs = "Failed to parse action from your response, make sure you provide a valid action."
            else:
                retry_count = 0  # reset on any successful parse so isolated truncations don't hard-stop a run
                logger.info("Step %d: %s", step_idx + 1, action)
                obs, done = self.env.step(action)

                if last_action is not None and last_action == action:
                    if repeat_action:
                        return False, "ERROR: Repeated action"
                    else:
                        obs = "The action is the same as the last one, you MUST provide a DIFFERENT SQL code or Python Code or different action. you MUST provide a DIFFERENT SQL code or Python Code or different action. you MUST provide a DIFFERENT SQL code or Python Code or different action."
                        repeat_action = True
                else:
                    obs, done = self.env.step(action)
                    last_action = action
                    repeat_action = False

            # --- DFC post-materialization steering (recharge001 only, gated) -------
            # Fire the checker AFTER the agent (re)builds the target with dbt, or when
            # it tries to Terminate. On violation (while retries remain) inject a
            # violation-specific observation so the agent revises the SQL and rebuilds,
            # rather than leaving the wrong `amount` in place / terminating on it.
            if (self.dfc_policy == "recharge001" and action is not None
                    and self.dfc_retries < self.dfc_max_retries):
                steered, obs, done = self._dfc_maybe_steer(action, obs, done)
                if steered:
                    last_action = None       # allow the rebuild without repeat-action tripping
                    repeat_action = False
                    step_idx += 1
                    continue                 # feed the RETRY obs to the next predict()

            if done:
                if isinstance(action, Terminate):
                    result = action.output
                logger.info("The task is done.")
                break
            step_idx += 1

        return done, result

    # --- DFC scoped policy helpers (recharge001 only) --------------------------
    def _dfc_run_check(self):
        """Run the recharge001 discount-amount checker against the produced DuckDB.

        /workspace is bind-mounted to env.mnt_dir on the host, so the agent's
        recharge.duckdb (with the materialized target + source tables) is readable
        here directly. Returns the checker verdict dict, or None on any failure
        (missing db, unreadable) so the hook simply doesn't steer in that case.
        """
        try:
            from spider_agent.agent.dfc_check import check_recharge001_discounts
            import os
            db = os.path.join(self.env.mnt_dir, "recharge.duckdb")
            if not os.path.exists(db):
                return None
            return check_recharge001_discounts(db)
        except Exception as e:
            logger.info("DFC check skipped (error): %s", e)
            return None

    @staticmethod
    def _is_dbt_build(action):
        """Heuristic: a Bash action that ran `dbt run`/`dbt build` (materializes the target)."""
        code = (getattr(action, "code", "") or "").lower()
        return "dbt" in code and ("run" in code or "build" in code)

    def _dfc_maybe_steer(self, action, obs, done):
        """Run the checker after a dbt build (or a Terminate) and steer on violation.

        Returns (steered, obs, done). When steered:
          - a dbt-build step: the violation-specific message is APPENDED to the build
            observation (done stays False);
          - a Terminate step: termination is blocked (done -> False) and the message
            becomes the next observation.
        Never writes/changes data; only feeds text back to the agent.
        """
        is_build = isinstance(action, Bash) and self._is_dbt_build(action)
        is_term = isinstance(action, Terminate)
        if not (is_build or is_term):
            return False, obs, done
        verdict = self._dfc_run_check()
        if verdict is None or verdict.get("status") != "violation":
            return False, obs, done
        self.dfc_retries += 1
        trigger = "terminate" if is_term else "dbt_build"
        self.dfc_events.append({"round": self.dfc_retries, "trigger": trigger, **verdict})
        logger.info("DFC RETRY %d/%d (%s): %s", self.dfc_retries, self.dfc_max_retries,
                    trigger, verdict["message"])
        msg = self._dfc_retry_message(verdict)
        if is_term:
            return True, msg, False
        return True, (str(obs) + "\n\n" + msg), False

    def _dfc_retry_message(self, verdict):
        """Build the violation-specific RETRY observation from the checker output."""
        rows = "; ".join(
            f"charge_id {v['charge_id']} ({v['title']}): found {v['amount_found']}, "
            f"expected {v['amount_expected']}"
            for v in verdict["violations"]
        )
        return (
            "DFC policy violation on recharge__charge_line_item_history: the `amount` for "
            "percentage discounts must be derived as round(value/100 * total_line_items_price, 2), "
            "not the raw discount value. The following rows have the raw value where the derived "
            f"amount is expected -> {rows}. Revise the model SQL (the discount CTE must convert "
            "percentage discounts using the charge's total_line_items_price) and rebuild with dbt run, "
            "then Terminate again."
        )

    def get_trajectory(self):
        trajectory = []
        for i in range(len(self.observations)):
            trajectory.append({
                "observation": self.observations[i],
                "thought": self.thoughts[i],
                "action": str(self.actions[i]),
                # "code": self.codes[i],
                "response": self.responses[i]
            })
        trajectory_log = {
            "Task": self.instruction,
            "system_message": self.system_message,
            "trajectory": trajectory
        }
        return trajectory_log


if __name__ == "__main__":
    agent = PromptAgent()
    response = """
BIGQUERY_EXEC_SQL(sql_query=\"\"\"
WITH purchase_users AS (
  SELECT DISTINCT user_pseudo_id
  FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
  WHERE event_name = 'purchase' AND _TABLE_SUFFIX BETWEEN '20201201' AND '20201231'
),
pageviews AS (
  SELECT user_pseudo_id, COUNT(*) AS pageviews
  FROM `bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*`
  WHERE event_name = 'page_view' AND _TABLE_SUFFIX BETWEEN '20201201' AND '20201231'
  GROUP BY user_pseudo_id
),
pageviews_by_user AS (
  SELECT 
    p.user_pseudo_id, 
    p.pageviews,
    CASE WHEN pu.user_pseudo_id IS NOT NULL THEN 'purchaser' ELSE 'non-purchaser' END AS user_type
  FROM pageviews p
  LEFT JOIN purchase_users pu ON p.user_pseudo_id = pu.user_pseudo_id
)
SELECT user_type, AVG(pageviews) AS avg_pageviews
FROM pageviews_by_user
GROUP BY user_type
\"\"\", is_save=True, save_path="avg_pageviews_dec_2020.csv")
"""

    response = """
BIGQUERY_EXEC_SQL(sql_query=\"\"\"
SELECT DISTINCT user_pseudo_id
FROM bigquery-public-data.ga4_obfuscated_sample_ecommerce.events_*
WHERE event_name = 'purchase' AND _TABLE_SUFFIX BETWEEN '20201201' AND '20201231'
\"\"\", is_save=False)
"""


    action = agent.parse_action(response)
    print(action)