"""Reminder and escalation engine.

Two engines run on a schedule, only inside business hours:

- `run_periodic_jobs` (POST /cron, hourly) applies the project-task rules
  A-H and F from `project_rules`.
- `run_independent_periodic_jobs` (POST /cron-fast, every 30 minutes) applies
  the independent-task rules I, I2 and J from `independent_rules`.

Rules are pure functions returning a `RuleResult`; the engine applies it
(task updates, outbound message, conversation row, audit log). Questions the
engine asks (`due_check`, `eta_request`, ...) are answered through the normal
handlers.
"""

from .base import RuleResult
from .engine import run_independent_periodic_jobs, run_periodic_jobs
from .independent_rules import INDEPENDENT_RULES
from .project_rules import PROJECT_RULES

__all__ = [
    "INDEPENDENT_RULES",
    "PROJECT_RULES",
    "RuleResult",
    "run_independent_periodic_jobs",
    "run_periodic_jobs",
]
