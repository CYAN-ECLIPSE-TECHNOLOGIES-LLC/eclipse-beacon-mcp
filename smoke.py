#!/usr/bin/env python3
"""Smoke: drive server.py over real stdio with a temp state file.
Ingests the synthetic fixtures (confirm), runs radar + gap, prints a
compact transcript. Uses NO real data and makes NO network calls."""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent


def call(proc, rid, name, args):
    proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid,
                                 "method": "tools/call",
                                 "params": {"name": name,
                                            "arguments": args}}) + "\n")
    proc.stdin.flush()
    resp = json.loads(proc.stdout.readline())
    return json.loads(resp["result"]["content"][0]["text"])


def main():
    with tempfile.TemporaryDirectory() as td:
        env = {"ECLIPSE_BEACON_STATE": str(Path(td) / "state.json"),
               "PATH": "/usr/bin:/bin:/usr/local/bin"}
        proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                text=True, env=env)
        try:
            proc.stdin.write(json.dumps(
                {"jsonrpc": "2.0", "id": 0, "method": "initialize",
                 "params": {"protocolVersion": "2025-03-26"}}) + "\n")
            proc.stdin.flush()
            init = json.loads(proc.stdout.readline())
            print("server:", init["result"]["serverInfo"])
            rid = 1
            for name in ("household.json", "auto.json", "homeowners.json",
                         "health.json", "term-life.json"):
                out = call(proc, rid, "policy_ingest",
                           {"path": str(ROOT / "fixtures" / name),
                            "confirm": True})
                rid += 1
                print(f"ingest {name}: {out['status']}")
            draft = call(proc, rid, "policy_ingest",
                         {"path": str(ROOT / "fixtures" / "auto.txt")})
            rid += 1
            print("re-ingest auto.txt (no confirm):", draft["status"],
                  "— graph unchanged")
            lst = call(proc, rid, "policy_list", {})
            rid += 1
            print("policies in graph:", lst["count"],
                  "| assets:", sorted(lst["assets"]))
            radar = call(proc, rid, "radar_report", {"as_of": "2026-10-25"})
            rid += 1
            print(f"radar @2026-10-25: {radar['count']} events")
            for e in radar["events"][:5]:
                print(f"  {e['date']} (+{e['days_until']}d) [{e['severity']}] "
                      f"{e['kind']} — {e['policy_type'] or 'calendar'}")
            gaps = call(proc, rid, "gap_report", {})
            print(f"gap findings: {gaps['count']}")
            for f in gaps["findings"]:
                print(f"  {f['kind']}: {f.get('asset_id') or f.get('carrier')}")
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
