"""Reminder and escalation engine: [Redacted].

The original system runs 11 scheduled rules behind two endpoints, `/cron`
(hourly) and `/cron-fast` (every 30 minutes), during business hours only.
They cover pre-deadline reminders, due-day checks, chasers for outstanding
replies from assignees and outside parties, late-completion escalation to
the task owner, and an escalation cap. A marker in the task's notes stops a
rule firing twice in the same cycle.

The rules, their thresholds, their wording and both endpoints are
[Redacted] from this showcase. What remains visible in the included code is
the interface they rely on: the `due_check` and `eta_request` conversation
purposes that handlers answer, the reminder markers in `workflow.py`, and
`workflow.clear_late_completion_state`, which handlers call when a deliberate
schedule change restarts a task's reminder cycle.
"""
