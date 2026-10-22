"""Post-deploy smoke test: GET /health, then a 3-call run that must reach completed.

Usage: python scripts/smoke.py [base_url]   (default http://localhost:8000)
"""

import sys
import time

import httpx

base = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
with httpx.Client(base_url=base, timeout=10) as client:
    health = client.get("/health").json()
    print("health:", health)
    if health["mode"] != "mock":
        sys.exit("refusing to smoke-test in real mode: it would place real calls")
    persona = client.get("/personas").json()[0]["id"]
    run = client.post("/runs", json={"persona_ids": [persona], "calls_per_persona": 3}).json()
    print("run:", run["id"])
    deadline = time.monotonic() + 60
    while (summary := client.get(f"/runs/{run['id']}").json())["status"] != "completed":
        if time.monotonic() > deadline:
            sys.exit(f"run did not complete: {summary}")
        time.sleep(0.5)
    print("status counts:", summary["status_counts"])
    print("smoke OK")
