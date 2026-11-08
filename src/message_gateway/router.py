"""Message processing: filters, the security gate, classification and dispatch.

should_skip -> gate (unknown sender: fixed reply, stop) -> idempotency marker
-> classify -> dispatch every intent into one reply buffer -> single reply
-> processed marker -> mark_processed
"""

from __future__ import annotations

import logging
from typing import Protocol

from .channel import Channel
from .classifier import ClassificationResult
from .context import HandlerContext, ReplyBuffer
from .gate import handle_unknown_sender
from .handlers import handler_for
from .models import Contact, Contacts, IncomingMessage, Project
from .records import load_contacts, load_projects, log_activity
from .store import Store
from .text import ack

log = logging.getLogger("message_gateway.router")


class SupportsClassify(Protocol):
    def classify(
        self, msg: IncomingMessage, sender: Contact, projects: dict[str, Project]
    ) -> ClassificationResult: ...


def dispatch_classification(
    *,
    channel: Channel,
    store: Store,
    msg: IncomingMessage,
    sender: Contact,
    classification: ClassificationResult,
    contacts: Contacts,
    projects: dict[str, Project],
) -> bool:
    """Run every intent's handler into one shared buffer and send one reply.

    Projects and contacts are reloaded before each follow-on intent, so
    [create_project, assign_task] sees the new project on the second hop. If
    a handler raises, the loop stops and a note tells the sender which item
    failed; earlier items stay applied. Returns False if dispatch stopped early.
    """
    lang = msg.language
    replies = ReplyBuffer(lang)
    intents = classification.intents or [classification.primary]
    completed = True
    for i, intent in enumerate(intents):
        if i > 0:
            try:
                projects = load_projects(store)
                contacts = load_contacts(store)
            except Exception:
                log.exception("reload between intents failed; continuing with stale snapshot")
        ctx = HandlerContext(
            channel=channel,
            store=store,
            msg=msg,
            sender=sender,
            intent=intent,
            contacts=contacts,
            projects=projects,
            replies=replies,
        )
        try:
            handler_for(intent.intent)(ctx)
        except Exception as e:
            log.exception("intent dispatch failed (intent=%s)", intent.intent)
            log_activity(
                store,
                "Intent dispatch error",
                sender.identity,
                f"intent={intent.intent}: {type(e).__name__}: {str(e)[:160]}",
            )
            replies.entries.append(
                (
                    ack(
                        f"Stopped after an error on item {i + 1} ({intent.intent}). "
                        "The earlier items above were processed; later items were skipped.",
                        f"توقفت بعد خطأ في البند {i + 1} ({intent.intent}). "
                        "البنود السابقة تم تنفيذها؛ البنود اللاحقة تم تخطيها.",
                        lang,
                    ),
                    None,
                )
            )
            completed = False
            break

    if replies.entries:
        channel.reply(msg, replies.plain, blocks=replies.entries)
    return completed


def process_message(
    msg: IncomingMessage, *, channel: Channel, store: Store, classifier: SupportsClassify
) -> None:
    """Entry point for a fully-formed inbound message."""
    skip, reason = channel.should_skip(msg)
    if skip:
        log_activity(store, f"{channel.name} message skipped", msg.sender_identity, reason)
        channel.mark_processed(msg)
        return

    contacts = load_contacts(store)
    sender = channel.lookup_sender(msg, contacts)

    # ─── Security gate: before any model call, no exceptions ──────────────
    if sender is None:
        admin = contacts.admin
        if not admin:
            # Not marked processed: the message stays in the inbox for a later drain.
            log_activity(
                store,
                "ERROR",
                "(agent)",
                f"Unknown {channel.name} sender {msg.sender_identity} "
                "but no admin in Contacts. Manual intervention required.",
            )
            return
        handle_unknown_sender(channel=channel, msg=msg, store=store, admin=admin)
        channel.mark_processed(msg)
        return

    # Written before classification so a concurrent delivery of the same
    # message sees it in flight (records.is_msg_id_processed).
    log_activity(
        store,
        "Message dispatch started",
        f"{sender.name} / msg_id:{msg.msg_id}",
        (f'Subject: "{msg.subject[:120]}". ' if msg.subject else "") + f"Channel: {channel.name}.",
    )

    projects = load_projects(store)
    classification = classifier.classify(msg, sender, projects)
    dispatch_classification(
        channel=channel,
        store=store,
        msg=msg,
        sender=sender,
        classification=classification,
        contacts=contacts,
        projects=projects,
    )
    primary = classification.primary
    intents_summary = (
        f"Intent: {primary.intent} (conf={primary.confidence:.2f}). " + primary.reasoning[:160]
    )
    if len(classification.intents) > 1:
        extras = ", ".join(f"{i.intent}({i.confidence:.2f})" for i in classification.intents[1:])
        intents_summary += f" [+{len(classification.intents) - 1}: {extras}]"
    log_activity(
        store,
        action=f"{channel.name} message processed",
        related=f"{sender.name} / msg_id:{msg.msg_id}",
        details=(f'Subject: "{msg.subject[:120]}". ' if msg.subject else "") + intents_summary,
    )
    channel.mark_processed(msg)
    log_activity(
        store,
        "Run completed (agent)",
        "(agent)",
        f"Webhook via {channel.name}; intents handled: {len(classification.intents)}.",
    )
