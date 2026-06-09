import json
import threading
from datetime import datetime, timezone


def _write_to_supabase(row: dict):
    try:
        from utils.supabase_client import supabase
        supabase.table("event_log").insert(row).execute()
    except Exception:
        pass  # stdout is the fallback; never raise


def log_event(user_id, event_type, event_data=None):
    try:
        now = datetime.now(timezone.utc).isoformat()
        uid = str(user_id) if user_id else None

        # Always print to stdout first — zero-latency, drain-captured
        payload = {
            "log_type": "USER_EVENT",
            "event_type": event_type,
            "user_id": uid,
            "event_data": event_data or {},
            "timestamp": now,
        }
        print(json.dumps(payload), flush=True)

        # Write to Supabase in a background thread — fire and forget
        row = {
            "event_type": event_type,
            "user_id": uid,
            "event_data": event_data or {},
            "created_at": now,
        }
        threading.Thread(target=_write_to_supabase, args=(row,), daemon=True).start()

    except Exception:
        pass  # never break a user request
