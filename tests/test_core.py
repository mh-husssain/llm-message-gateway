"""Schema, store, loaders, text and time helpers."""

from __future__ import annotations

import pytest

from fakes import ALICE, MANAGER, seed_task
from message_gateway.clock import (
    format_display_date,
    iso_to_sheet_date,
    local_tz,
    parse_deadline_iso,
    parse_sheet_date,
    timezone_name,
)
from message_gateway.config import ConfigError, Settings
from message_gateway.models import IncomingMessage, IndependentTask, normalize_status
from message_gateway.records import (
    append_independent_task,
    load_contacts,
    load_independent_tasks,
    load_raw_contact_index,
    load_tasks,
    update_task_fields,
)
from message_gateway.schema import ALL_TABS, CONTACTS, INDEPENDENT_TASKS, TASKS
from message_gateway.store import InMemoryStore, Store
from message_gateway.store.memory import col_index
from message_gateway.text import (
    has_completion_keyword,
    humanize_status,
    next_task_id,
    normalize_email,
    normalize_phone,
    subject_line,
)
from message_gateway.workflow import find_task_by_hint, resolve_assignee

# ─── Schema ────────────────────────────────────────────────────────────────


def test_every_tab_has_sequential_letters_and_unique_names():
    for tab in ALL_TABS:
        assert [c.letter for c in tab.columns] == [chr(ord("A") + i) for i in range(tab.num_cols)]
        assert len(set(tab.header_row)) == tab.num_cols
    assert len(ALL_TABS) == 8


def test_schema_ranges():
    assert TASKS.range_a2 == "Tasks!A2:U" and TASKS.num_cols == 21
    assert CONTACTS.range_full == "Contacts!A:G"
    assert INDEPENDENT_TASKS.last_letter == "P" and INDEPENDENT_TASKS.num_cols == 16
    assert TASKS.col_index["status"] == "K"


def test_workbook_generator(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    from message_gateway.workbook import generate

    out = tmp_path / "blank.xlsx"
    generate(out)
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Schema_README"] + [t.name for t in ALL_TABS]
    ws = wb["Tasks"]
    assert [c.value for c in ws[1]] == TASKS.header_row
    assert ws["A1"].comment is not None
    assert ws.freeze_panes == "A2"


# ─── In-memory store ───────────────────────────────────────────────────────


def test_in_memory_store_satisfies_the_protocol_and_ranges():
    s = InMemoryStore.with_schema()
    assert isinstance(s, Store)
    s.append("Tasks!A:U", ["T-0001", "", "P-0001"])
    assert s.read("Tasks!A2:C") == [["T-0001", "", "P-0001"]]
    assert s.read("Tasks!A1:A1") == [["task_id"]]
    s.update("Tasks!K2", [["done"]])
    assert s.tabs["Tasks"][1][10] == "done"
    s.batch_update([("Tasks!A2", [["T-0002"]]), ("Tasks!B3", [["x"]])])
    assert s.tabs["Tasks"][1][0] == "T-0002" and s.tabs["Tasks"][2][1] == "x"
    with pytest.raises(ValueError):
        s.update("Tasks!A:A", [["x"]])
    with pytest.raises(ValueError):
        s.read("not a range")
    assert (col_index("A"), col_index("Z"), col_index("AA")) == (0, 25, 26)


def test_task_round_trip_and_in_place_update(store):
    seed_task(store, task_id="T-0001", escalation_count=3, notes="n")
    t = load_tasks(store)[0]
    assert t.row_index == 2 and t.escalation_count == 3
    update_task_fields(store, t, status="completed", escalation_count="0")
    assert t.escalation_count == 0
    t2 = load_tasks(store)[0]
    assert (t2.status, t2.escalation_count) == ("completed", 0)
    with pytest.raises(KeyError):
        update_task_fields(store, t, not_a_field="x")


def test_update_requires_a_loaded_row(store):
    t = seed_task(store, task_id="T-0001")  # never loaded: row_index 0
    with pytest.raises(ValueError):
        update_task_fields(store, t, status="completed")


def test_legacy_statuses_are_normalised(store):
    store.append("Tasks!A:U", ["T-0002", "", "", "", "", "", "", "", "d", "1", "In Progress"])
    store.append("Tasks!A:U", ["T-0003", "", "", "", "", "", "", "", "d", "x", "New"])
    rows = {t.task_id: t for t in load_tasks(store)}
    assert rows["T-0002"].status == "in_progress"
    assert rows["T-0003"].status == "assigned" and rows["T-0003"].sub_step_index == 0
    assert normalize_status("Canceled") == "cancelled" and normalize_status("") == ""


def test_independent_task_round_trip(store):
    append_independent_task(
        store,
        IndependentTask(
            task_id="I-0100",
            creator_id=MANAGER,
            creator_name="Manager One",
            assignee_id=ALICE,
            assignee_name="Alice",
            description="d",
            status="assigned",
            deadline="07/05/2026 16:00:00",
            pre_deadline_reminder_minutes="",
            last_reminder_sent_at="",
            escalation_count=0,
            escalated_at="",
            completed_at="",
            original_request_msg_id="",
            completion_msg_id="",
            notes="",
        ),
    )
    t = load_independent_tasks(store)[0]
    assert (t.task_id, t.assignee_name, t.row_index) == ("I-0100", "Alice", 2)
    assert t.deadline_obj.hour == 16 and t.is_open


def test_contacts_loader_skips_inactive_and_indexes_aliases(store):
    store.append(
        "Contacts!A:G",
        ["", "Erin", "erin@example.com, Erin.Alt@example.com", "", "", "employee", "yes"],
    )
    contacts = load_contacts(store)
    assert contacts.by_email("ERIN.alt@example.com").name == "Erin"
    assert contacts.by_email("carol@example.com") is None  # inactive
    assert contacts.admin.name == "Admin One"
    raw = load_raw_contact_index(store)
    assert raw["carol@example.com"]["active"] == "no"
    assert raw["15555550199"]["name"] == "Carol"


# ─── Lookup helpers ────────────────────────────────────────────────────────


def test_resolve_assignee(store):
    contacts = load_contacts(store)
    assert resolve_assignee("ali", contacts)[0].name == "Alice"
    assert resolve_assignee("", contacts) == (None, "no_name")
    assert resolve_assignee("zed", contacts) == (None, "not_found")
    assert resolve_assignee("one", contacts) == (None, "ambiguous")


def test_find_task_by_hint(store):
    seed_task(store, task_id="T-0001", description="paint the fence")
    seed_task(store, task_id="T-0002", description="fix the gate")
    tasks = load_tasks(store)
    assert find_task_by_hint(tasks, ALICE, task_id_hint="T-0002")[0].task_id == "T-0002"
    assert find_task_by_hint(tasks, ALICE, task_id_hint="T-0404") == (None, "id_not_found")
    assert find_task_by_hint(tasks, ALICE, description_hint="FENCE")[0].task_id == "T-0001"
    assert find_task_by_hint(tasks, ALICE, description_hint="the") == (None, "ambiguous")
    assert find_task_by_hint(tasks, ALICE, description_hint="roof") == (
        None,
        "description_not_found",
    )
    assert find_task_by_hint(tasks, ALICE) == (None, "ambiguous")
    assert find_task_by_hint(tasks, MANAGER) == (None, "no_open_tasks")
    assert find_task_by_hint(tasks, MANAGER, restrict_to_assignee=False) == (None, "ambiguous")


# ─── Text and time ─────────────────────────────────────────────────────────


def test_normalisation_and_keywords():
    assert normalize_email("Alice <ALICE@example.com>") == "alice@example.com"
    assert normalize_email("") == ""
    assert normalize_phone("+1 (555) 555-0123") == "15555550123"
    assert has_completion_keyword("All DONE here") and has_completion_keyword("تم")
    assert not has_completion_keyword("still working")


def test_ids_and_status_words():
    assert next_task_id(["T-0001", "T-0009", "X-1", "T-12"]) == "T-0010"
    assert next_task_id([]) == "T-0001"
    assert humanize_status("awaiting_assignee_completion") == "awaiting completion"
    assert humanize_status("in_progress") == "in progress"
    assert humanize_status("totally_made_up") == "totally made up"
    assert humanize_status("awaiting_assignee_completion", "ar") == "بانتظار الإكمال"


def test_subject_prefix_is_configurable(monkeypatch):
    assert subject_line("x") == "[Gateway] x"
    monkeypatch.setenv("GATEWAY_SUBJECT_PREFIX", "[Ops]")
    assert subject_line("x") == "[Ops] x"
    monkeypatch.setenv("GATEWAY_SUBJECT_PREFIX", "")
    assert subject_line("x") == "x"


def test_dates():
    assert iso_to_sheet_date("2026-05-05") == "05/05/2026"
    assert iso_to_sheet_date("05/05/2026") == "" and iso_to_sheet_date("") == ""
    assert parse_sheet_date("2026-04-18 00:00:00").day == 18
    assert parse_sheet_date("bad") is None
    assert format_display_date("2026-04-18 00:00:00") == "18/04/2026"
    assert format_display_date("whenever") == "whenever"


def test_timezone_defaults_to_utc_and_is_configurable(monkeypatch):
    assert timezone_name() == "UTC"
    dt = parse_deadline_iso("2026-05-07T16:00:00+03:00")
    assert dt.hour == 13 and dt.utcoffset().total_seconds() == 0
    assert parse_deadline_iso("2026-05-07T13:00:00Z").hour == 13
    assert parse_deadline_iso("2026-05-07T16:00:00").hour == 16  # naked = local
    assert parse_deadline_iso("not a date") is None and parse_deadline_iso(None) is None

    monkeypatch.setenv("GATEWAY_TIMEZONE", "Europe/Berlin")
    local_tz.cache_clear()
    assert timezone_name() == "Europe/Berlin"
    assert parse_deadline_iso("2026-05-07T12:00:00Z").hour == 14  # CEST


def test_message_language():
    m = IncomingMessage(
        msg_id="1", sender_identity="a", sender_display_name="", subject="", body="مرحبا"
    )
    assert m.language == "ar"
    assert IncomingMessage("1", "a", "", "Hello", "there").language == "en"


def test_settings_from_env():
    s = Settings.from_env({"GATEWAY_SHEET_ID": " abc ", "CLASSIFIER_CONFIDENCE_THRESHOLD": "0.7"})
    assert s.sheet_id == "abc" and s.classifier_threshold == 0.7 and s.timezone == "UTC"
    assert s.gmail_watch_label_ids == "INBOX"
    with pytest.raises(ConfigError, match="GMAIL_USER"):
        s.require("gmail_user")
