"""One handler per intent. Each takes a `HandlerContext`, re-validates what the
model extracted, checks the sender's permission, updates the store and writes
its section of the combined reply."""

from __future__ import annotations

from collections.abc import Callable

from ..context import HandlerContext
from ..records import log_activity
from ..text import subject_line
from .assignment import handle_assign_task
from .completion import handle_task_completion
from .confirmation import handle_confirmation_response
from .contacts import handle_add_contact
from .extensions import handle_delay_acknowledgment, handle_request_extension
from .external import handle_external_completion_confirmation, handle_external_eta_response
from .info import handle_request_info
from .projects import handle_create_project
from .reports import handle_end_of_day_report
from .updates import handle_task_update

Handler = Callable[[HandlerContext], None]

HANDLERS: dict[str, Handler] = {
    "assign_task": handle_assign_task,
    "task_completion": handle_task_completion,
    "task_update": handle_task_update,
    "completion_confirmation_response": handle_confirmation_response,
    "request_extension": handle_request_extension,
    "delay_acknowledgment": handle_delay_acknowledgment,
    "external_eta_response": handle_external_eta_response,
    "external_completion_confirmation": handle_external_completion_confirmation,
    "request_info": handle_request_info,
    "create_project": handle_create_project,
    "add_contact": handle_add_contact,
    "end_of_day_report": handle_end_of_day_report,
}


def handle_unclear(ctx: HandlerContext) -> None:
    """Ask the sender to rephrase and forward the message to the admin."""
    msg, sender = ctx.msg, ctx.sender
    ctx.reply(
        "I'm not sure how to handle that. Could you rephrase, or contact management directly?",
        "غير واضح ماذا تقصد. يرجى إعادة الصياغة أو التواصل مع الإدارة.",
    )
    admin = ctx.contacts.admin
    if admin:
        ctx.channel.send_new(
            admin,
            subject=subject_line(f"Unclear {ctx.channel.name} message from {sender.name}"),
            body=(
                f"From: {sender.name} ({msg.sender_identity})\n"
                + (f"Subject: {msg.subject}\n" if msg.subject else "")
                + f"\n{msg.body[:3000]}"
            ),
        )
    log_activity(
        ctx.store,
        "Unclear message",
        ctx.sender_id,
        f'Subject/body: "{(msg.subject or msg.body)[:120]}". '
        f"Confidence={ctx.intent.confidence:.2f}.",
    )


def handler_for(intent_name: str) -> Handler:
    return HANDLERS.get(intent_name, handle_unclear)


__all__ = ["HANDLERS", "Handler", "handle_unclear", "handler_for"]
