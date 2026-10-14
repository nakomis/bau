#!/usr/bin/env python3
"""Pretend to be `docker run ... claude -p`. Behaviour is chosen by FAKE_MODE."""

import json
import os
import sys
import time

mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]
if argv and argv[0] == "kill":
    open(os.environ["FAKE_KILL_LOG"], "a").write("killed\n")
    sys.exit(0)

brief = sys.stdin.read()
open(os.environ["FAKE_BRIEF_LOG"], "w").write(brief)


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def rl(five, seven, status="allowed"):
    emit(
        {
            "type": "rate_limit_event",
            "rate_limit_info": {
                "status": status,
                "unifiedWindows": {
                    "five_hour": {"utilization": five, "resetsAt": 1},
                    "seven_day": {"utilization": seven, "resetsAt": 2},
                },
            },
        }
    )


emit({"type": "system", "subtype": "init"})
if mode == "ok":
    rl(0.2, 0.3)
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Opened https://github.com/x/y/pull/1",
            "total_cost_usd": 0.42,
        }
    )
elif mode == "too_big":
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Looked.\nBAU_TOO_BIG: schema change",
            "total_cost_usd": 0.1,
        }
    )
elif mode == "limit":
    rl(0.9, 0.9, "rejected")
    emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "You've hit your weekly limit · resets Mon",
            "total_cost_usd": 0,
        }
    )
elif mode == "five_hour":
    rl(0.85, 0.3)
    time.sleep(5)
elif mode == "slow":
    time.sleep(5)
elif mode == "crash":
    sys.exit(3)
