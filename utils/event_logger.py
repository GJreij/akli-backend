import json
import traceback
from datetime import datetime, timezone


def log_event(user_id, event_type, event_data=None):
    try:
        payload = {
            "log_type": "USER_EVENT",
            "event_type": event_type,
            "user_id": str(user_id) if user_id else None,
            "event_data": event_data or {},
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        print(json.dumps(payload), flush=True)
    except Exception:
        pass  # never break a user request
