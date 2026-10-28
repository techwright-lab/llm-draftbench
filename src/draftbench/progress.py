"""Content-free live pilot progress, independent of provider output streams."""

import sys
import time


def terminal_progress():
    started = time.monotonic()

    def emit(event):
        elapsed = time.monotonic() - started
        campaign = event["campaign"]
        usage = event.get("usage") or {}
        estimate = usage.get("estimated_usd")
        line = (
            f"[{elapsed:8.1f}s] {event['model']} | {event['stage']} | "
            f"{event['state']} | attempts={campaign['reservation_count']} | "
            f"reserved_usd={campaign['reserved_usd']} | "
            f"input_tokens={usage.get('input_tokens')} | output_tokens={usage.get('output_tokens')} | "
            f"estimated_usd={estimate if estimate is not None else 'unknown'} (not billed cost)"
        )
        try:
            print(line, file=sys.stderr, flush=True)
        except OSError:
            # A closed progress pipe must not interrupt a paid request chain.
            pass

    return emit
