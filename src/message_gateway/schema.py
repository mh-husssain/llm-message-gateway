"""Spreadsheet schema: the single source of truth for every tab and column.

Runtime code imports ranges and column letters from here instead of
hard-coding them, so a schema change in one place flows to the loaders, the
writers and the workbook generator (`message_gateway.workbook`).

Standard library only, so tooling can import it without the API clients.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Column:
    """One column in a tab. `letter` is the spreadsheet column (A, B, ...)."""

    letter: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class TabSchema:
    """One tab in the spreadsheet."""

    name: str
    columns: tuple[Column, ...]
    description: str = ""

    @property
    def header_row(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def col_index(self) -> dict[str, str]:
        """{field_name: letter}, used by code that updates individual cells."""
        return {c.name: c.letter for c in self.columns}

    @property
    def num_cols(self) -> int:
        return len(self.columns)

    @property
    def last_letter(self) -> str:
        return self.columns[-1].letter

    @property
    def range_a2(self) -> str:
        """'Tab!A2:<last>': the read-from-row-2 range used by loaders."""
        return f"{self.name}!A2:{self.last_letter}"

    @property
    def range_full(self) -> str:
        """'Tab!A:<last>': the full-column range used by appends."""
        return f"{self.name}!A:{self.last_letter}"


CONTACTS = TabSchema(
    name="Contacts",
    description=(
        "Verified staff. The classifier is only invoked for senders who match "
        "an active row here by email or phone. Edited by hand or via add_contact."
    ),
    columns=(
        Column("A", "phone", "Digits only; normalised at load time."),
        Column("B", "name", "Display name."),
        Column("C", "email", "Lowercased at load time; comma-separated aliases allowed."),
        Column("D", "division", "Free-text department label."),
        Column("E", "role", "Free-text role label."),
        Column("F", "permission_level", "admin | manager | employee."),
        Column("G", "active", "yes | no. Only 'yes' rows are loaded."),
    ),
)

PROJECTS = TabSchema(
    name="Projects",
    description="Active and historical projects.",
    columns=(
        Column("A", "Project_name", "Human-readable project name."),
        Column("B", "Project_ID", "P-XXXX (zero-padded)."),
        Column("C", "Status", "active | completed | paused."),
        Column("D", "Manager", "Manager name."),
        Column("E", "Start_date", "Free-form date."),
        Column("F", "Notes", "Free text."),
    ),
)

ACTIVITY_LOG = TabSchema(
    name="Activity_Log",
    description=(
        "Append-only audit trail. Message-ID markers written here make "
        "processing idempotent; do not edit existing rows."
    ),
    columns=(
        Column("A", "Timestamp", "dd/mm/yyyy HH:MM:SS in the configured timezone."),
        Column("B", "Action", "Verb-style label, e.g. 'Task assigned'."),
        Column("C", "Related_to", "Subject / sender / task id."),
        Column("D", "Details", "Free-text details."),
    ),
)

UNKNOWN_EMAILS = TabSchema(
    name="Unknown_Emails",
    description=(
        "Email senders not in Contacts. The fixed reply has already gone out "
        "and the admin has been alerted; rows here await manual review."
    ),
    columns=(
        Column("A", "Timestamp", "TS_FMT."),
        Column("B", "Email", "Sender email."),
        Column("C", "From_name", "Display name as the channel reported it."),
        Column("D", "Subject", "Email subject."),
        Column("E", "Their_message", "Body excerpt."),
        Column("F", "Our_response", "The fixed reply that was sent."),
        Column("G", "Forwarded_to", "Admin contact alerted."),
        Column("H", "Action_needed", "Review status."),
    ),
)

UNKNOWN_MESSAGES = TabSchema(
    name="Unknown_Messages",
    description=(
        "WhatsApp/SMS analogue of Unknown_Emails. Kept for schema parity; "
        "the WhatsApp adapter is not part of this repository."
    ),
    columns=(
        Column("A", "Timestamp", "TS_FMT."),
        Column("B", "Phone_number", "Sender phone in '+<digits>' display form."),
        Column("C", "WhatsApp_name", "Display name."),
        Column("D", "Their_message", "Body excerpt."),
        Column("E", "Our_response", "The fixed reply that was sent."),
        Column("F", "Forwarded_to", "Admin contact alerted."),
        Column("G", "Action_needed", "Review status."),
    ),
)

TASKS = TabSchema(
    name="Tasks",
    description=(
        "Workflow tasks. A multi-step assignment produces one parent row "
        "(sub_step_index=0) plus N child rows. Later steps stay 'blocked' "
        "until the predecessor completes."
    ),
    columns=(
        Column("A", "task_id", "T-XXXX (zero-padded)."),
        Column("B", "parent_task_id", "Empty for root rows."),
        Column("C", "project_id", "P-XXXX."),
        Column("D", "project_name", "Denormalised for a readable sheet view."),
        Column("E", "owner_id", "Normalised email/phone of the creator."),
        Column("F", "owner_name", ""),
        Column("G", "assignee_id", "Normalised email/phone; comma-separated for joint tasks."),
        Column("H", "assignee_name", ""),
        Column("I", "description", "What to do (per step)."),
        Column("J", "sub_step_index", "0=root, 1=first sub-step, ..."),
        Column(
            "K",
            "status",
            "assigned | in_progress | awaiting_assignee_completion | blocked | "
            "delayed | awaiting_owner_confirmation | awaiting_assignee_eta | "
            "awaiting_external_completion | completed | cancelled | escalated.",
        ),
        Column("L", "due_date", "dd/mm/yyyy."),
        Column("M", "external_party", "Outside organisation the step involves."),
        Column("N", "awaiting_response_from", "Identity the task is blocked on."),
        Column("O", "awaiting_response_since", "TS_FMT."),
        Column("P", "last_reminder_sent_at", "Reminder dedupe key (TS_FMT)."),
        Column("Q", "escalation_count", "Increments on each no-response cycle."),
        Column("R", "completed_at", "TS_FMT."),
        Column("S", "original_request_msg_id", "Ties the task back to the source message."),
        Column("T", "completion_msg_id", "Audit: which message marked it done."),
        Column("U", "notes", "Free-text history; also stores PROPOSAL: markers."),
    ),
)

TASK_CONVERSATIONS = TabSchema(
    name="Task_Conversations",
    description=(
        "Pairs outbound questions with inbound replies. The most recent "
        "outbound row with awaiting_reply=true for a participant is what their "
        "next YES/NO answers; no model disambiguation is needed."
    ),
    columns=(
        Column("A", "conv_id", "C-XXXX."),
        Column("B", "task_id", "Linked task (T-XXXX or I-XXXX)."),
        Column("C", "direction", "outbound | inbound."),
        Column("D", "participant_id", "Email or phone."),
        Column("E", "participant_role", "owner | assignee | external | admin."),
        Column("F", "channel", "Channel name, e.g. email."),
        Column("G", "msg_id", "Channel-specific message id."),
        Column("H", "timestamp", "TS_FMT."),
        Column(
            "I",
            "purpose",
            "assignment | due_check | eta_request | owner_confirmation_request | "
            "register_project_request | eod_log | ...",
        ),
        Column("J", "body_excerpt", "First 200 chars (full payload for parked requests)."),
        Column("K", "awaiting_reply", "true | false."),
    ),
)

INDEPENDENT_TASKS = TabSchema(
    name="Independent_Tasks",
    description=(
        "Project-less action items: one assignee, a hard deadline with minute "
        "precision, an instant notification on creation, and reminders from the "
        "/cron-fast engine (two post-deadline chasers, then one creator notice)."
    ),
    columns=(
        Column("A", "task_id", "I-XXXX (zero-padded)."),
        Column("B", "creator_id", "Email/phone of the assigning manager."),
        Column("C", "creator_name", ""),
        Column("D", "assignee_id", "Email/phone of the worker."),
        Column("E", "assignee_name", ""),
        Column("F", "description", "What needs to be done."),
        Column(
            "G",
            "status",
            "assigned | awaiting_assignee_completion | completed | cancelled | escalated.",
        ),
        Column("H", "deadline", "dd/mm/yyyy HH:MM:SS in the configured timezone."),
        Column(
            "I",
            "pre_deadline_reminder_minutes",
            "Optional int: interval for pre-deadline reminders; empty = none.",
        ),
        Column("J", "last_reminder_sent_at", "Reminder dedupe key (TS_FMT)."),
        Column("K", "escalation_count", "Reminder cycle counter."),
        Column("L", "escalated_at", "TS_FMT when the creator was notified."),
        Column("M", "completed_at", "TS_FMT."),
        Column("N", "original_request_msg_id", "Source channel message id."),
        Column("O", "completion_msg_id", "Audit: which message marked it done."),
        Column("P", "notes", "Free-text history."),
    ),
)

ALL_TABS: tuple[TabSchema, ...] = (
    CONTACTS,
    PROJECTS,
    ACTIVITY_LOG,
    UNKNOWN_EMAILS,
    UNKNOWN_MESSAGES,
    TASKS,
    TASK_CONVERSATIONS,
    INDEPENDENT_TASKS,
)
