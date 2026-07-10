__artifacts_v2__ = {
    "chatParticipants": {
        "name": "Chat Participants (All Apps)",
        "description": "Aggregates every unique chat participant (sender or recipient) found across all "
                        "messaging artifacts iLEAPP already parses (SMS/iMessage, WhatsApp, Teams, Discord, "
                        "Telegram, Slack, Signal-like apps, etc.), tagged with the app each one came from",
        "author": "Raymond",
        "creation_date": "2026-07-10",
        "last_update_date": "2026-07-10",
        "requirements": "none",
        "category": "Chat Participants",
        "notes": "Runs each chat artifact's own parsing function and reads its declared "
                 "data_views['conversation'] metadata (senderColumn/directionColumn/etc.) to identify "
                 "participants, instead of reimplementing per-app parsing logic. Any artifact module that "
                 "declares a conversation view with a senderColumn is picked up automatically, so newly "
                 "added chat parsers show up here with no changes to this module.",
        "paths": None,
        "output_types": "standard",
        "artifact_icon": "users",
    }
}

import os
import pathlib
import shutil

from scripts.context import Context
from scripts.ilapfuncs import artifact_processor, logfunc
from scripts.plugin_loader import PluginLoader, PLUGINPATH

_SELF_MODULE_STEM = "chatParticipants"


def _get_raw_function(mod, func_name):
    """Return the undecorated parsing function for func_name, or None."""
    for item_name in dir(mod):
        item = getattr(mod, item_name, None)
        if item_name == func_name and callable(item) and hasattr(item, "__wrapped__"):
            return item.__wrapped__
    return None


def _iter_chat_source_modules():
    """Yield (module_name, func_name, artifact_info, conversation, raw_func) for every
    artifact that declares a conversation data_view with a senderColumn."""
    for py_file in sorted(pathlib.Path(PLUGINPATH).glob("*.py")):
        if py_file.stem in ("__init__", _SELF_MODULE_STEM):
            continue
        try:
            mod = PluginLoader.load_module_lazy(py_file)
            artifacts_v2 = getattr(mod, "__artifacts_v2__", None)
        except Exception as ex:
            # A module may fail to import in this environment (e.g. an optional
            # dependency isn't installed); skip it rather than aborting discovery.
            logfunc(f"chatParticipants: could not load {py_file.name}: {ex}")
            continue

        if not artifacts_v2:
            continue

        for func_name, artifact_info in artifacts_v2.items():
            conversation = (artifact_info.get("data_views") or {}).get("conversation")
            if not conversation or not conversation.get("senderColumn"):
                continue

            raw_func = _get_raw_function(mod, func_name)
            if raw_func is None:
                continue

            yield func_name, artifact_info, conversation, raw_func


def _resolve_participant(row, header_index, conversation):
    """Return (participant_label, 'Sent'|'Received') for a parsed message row, or (None, None)."""
    direction_idx = header_index.get(conversation.get("directionColumn"))
    is_outgoing = (
        direction_idx is not None
        and row[direction_idx] == conversation.get("directionSentValue")
    )

    if is_outgoing:
        label_idx = header_index.get(conversation.get("sentMessageLabelColumn"))
        if label_idx is not None and row[label_idx]:
            return row[label_idx], "Sent"
        return conversation.get("sentMessageStaticLabel") or "Device Owner", "Sent"

    sender_idx = header_index.get(conversation.get("senderColumn"))
    if sender_idx is None or not row[sender_idx]:
        return None, None
    return row[sender_idx], "Received"


@artifact_processor
def chatParticipants(context):
    seeker = context.get_seeker()
    own_artifact_info = context.get_artifact_info()
    own_module_name = context.get_module_name()
    own_artifact_name = context.get_artifact_name()
    own_report_folder = context.get_report_folder()

    # A few source parsers (e.g. sms.py) write their own ad-hoc HTML report directly
    # from inside the parsing function, not just through the @artifact_processor
    # decorator's output step. Point report_folder at a scratch dir while re-running
    # them so that ends up here instead of polluting another artifact's report folder.
    scratch_folder = os.path.join(own_report_folder, '_source_rerun_scratch')

    participants = {}
    source_paths = []

    for func_name, artifact_info, conversation, raw_func in _iter_chat_source_modules():
        paths = artifact_info.get("paths") or ()
        if isinstance(paths, str):
            paths = (paths,)

        files_found = []
        for pattern in paths:
            files_found.extend(seeker.search(pattern))
        if not files_found:
            continue  # app not present in this extraction

        Context.set_files_found(files_found)
        Context.set_artifact_info(artifact_info)
        Context.set_module_name(raw_func.__module__.split(".")[-1])
        Context.set_artifact_name(artifact_info.get("name", func_name))
        os.makedirs(scratch_folder, exist_ok=True)
        Context.set_report_folder(scratch_folder)

        try:
            headers, rows, source_path = raw_func(Context)
        except Exception as ex:
            logfunc(f"chatParticipants: {func_name} raised {ex}, skipping")
            continue

        if isinstance(rows, tuple):
            rows = rows[0]  # (data_list, html_data_list) form -> keep the raw data_list

        if not rows:
            continue

        header_names = [h[0] if isinstance(h, tuple) else h for h in headers]
        header_index = {name: i for i, name in enumerate(header_names)}
        time_idx = header_index.get(conversation.get("timeColumn"))

        app_name = artifact_info.get("category", "")
        artifact_display_name = artifact_info.get("name", func_name)

        for row in rows:
            participant, role = _resolve_participant(row, header_index, conversation)
            if not participant:
                continue

            timestamp = row[time_idx] if time_idx is not None else None
            stats = participants.setdefault((app_name, participant), {
                "artifact": artifact_display_name,
                "sent": 0,
                "received": 0,
                "first": timestamp,
                "last": timestamp,
            })
            stats["sent" if role == "Sent" else "received"] += 1
            if timestamp:
                if not stats["first"] or timestamp < stats["first"]:
                    stats["first"] = timestamp
                if not stats["last"] or timestamp > stats["last"]:
                    stats["last"] = timestamp

        if source_path:
            source_paths.append(str(source_path))

    Context.set_artifact_info(own_artifact_info)
    Context.set_module_name(own_module_name)
    Context.set_artifact_name(own_artifact_name)
    Context.set_report_folder(own_report_folder)
    shutil.rmtree(scratch_folder, ignore_errors=True)

    data_list = []
    for (app_name, participant), stats in sorted(participants.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        if stats["sent"] and stats["received"]:
            role = "Sender & Recipient"
        elif stats["sent"]:
            role = "Sent Only"
        else:
            role = "Received Only"
        data_list.append((
            participant,
            app_name,
            stats["artifact"],
            role,
            stats["sent"] + stats["received"],
            stats["first"],
            stats["last"],
        ))

    data_headers = (
        "Participant",
        "App",
        "Source Artifact",
        "Role",
        "Message Count",
        ("First Message", "datetime"),
        ("Last Message", "datetime"),
    )

    return data_headers, data_list, "\n".join(source_paths)
