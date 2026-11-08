"""Sender security gate. Pure Python; the model is never involved.

A sender who does not match an active contact gets a fixed reply, is logged
(channel-specific row plus Activity_Log) and triggers an admin alert. The
message never reaches the classifier, the handlers or task data.
"""

from __future__ import annotations

from .channel import Channel
from .models import Contact, IncomingMessage
from .records import log_activity
from .store import Store
from .text import subject_line

# Constant, never edited, never interpolated: nothing from the inbound message
# (or from the store) can leak into what an unknown sender receives.
LOCKED_UNKNOWN_REPLY = (
    "Hello, thank you for reaching out. Your message has been received "
    "and logged. A team member will get back to you if needed."
)


def handle_unknown_sender(
    *, channel: Channel, msg: IncomingMessage, store: Store, admin: Contact
) -> None:
    # 1. Fixed reply (the channel decides how to deliver it, e.g. threaded).
    channel.reply(msg, LOCKED_UNKNOWN_REPLY)

    # 2. Channel-specific log row (e.g. Unknown_Emails).
    channel.log_unknown_row(store, msg, LOCKED_UNKNOWN_REPLY, admin)

    # 3. Channel-agnostic audit entry.
    log_activity(
        store,
        action=channel.unknown_log_action,
        related=f"{msg.sender_identity} / {msg.sender_display_name or '(no name)'}",
        details=(f'Subject: "{msg.subject}". ' if msg.subject else "")
        + (f"Sent locked auto-reply. Logged via {channel.name}. Status PENDING_MANAGER_REVIEW."),
    )

    # 4. Admin alert with the original message.
    subject = subject_line(f"Unknown {channel.name} sender: {msg.sender_identity}")
    body = (
        f"Unknown contact {msg.sender_display_name} ({msg.sender_identity}) "
        f"sent a message via {channel.name}.\n\n"
        "Auto-replied with locked template. Reply with explicit instructions "
        "if you want a custom response sent.\n\n"
        "───────────────────────────────────\n"
    )
    if msg.subject:
        body += f"Subject: {msg.subject}\n\n"
    body += msg.body[:3000]
    channel.send_new(admin, subject=subject, body=body)
