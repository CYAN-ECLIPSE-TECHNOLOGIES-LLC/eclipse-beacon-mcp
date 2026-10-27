#!/usr/bin/env python3
# Eclipse Beacon MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai

"""Eclipse Beacon MCP — v0.1.0, phase B0 (personal edition).

Powered by Cyan Eclipse Technologies (cetech.ai).

Eclipse Beacon — the insurance watcher: it watches your household's
policies, signals expiries before they happen, and prepares the actions
you approve. It does not insure, bind, or advise.

B0 SCOPE — READ/WATCH ONLY:
  * policy_ingest: parse declarations-page-style documents (JSON or
    simple text) from LOCAL files. Every extracted field carries
    provenance {source, locator, confidence}. Merges are PROPOSE-ONLY
    by default — nothing enters the graph without an explicit
    confirm=true from the owner. Drafts are never silently merged.
  * A local policy graph (policies, covered assets/persons, limits,
    deductibles, premiums, all dates) in a JSON store.
  * expiry_radar: ranked upcoming events — renewals, premium dues,
    grace-period ends, escrow proof deadlines, and static enrollment
    calendar entries (ACA / Medicare, flagged [verify]).
  * gap_report: read-only analysis — uncovered assets, duplicate
    coverage, premium spikes vs the prior term.

OUT OF SCOPE in B0 (by spec, not by omission): renewal shopping,
binding, cancelling, paying, claims filing, ANY network call, ANY real
personal data. There is deliberately no code path here that contacts a
carrier, an agency, or the internet. Fixtures are synthetic.

THE VIGIL RULE (encoded, tested): alert windows are owner-configurable,
but a configuration change may only TIGHTEN a window — widen its lead
time so the watcher alerts EARLIER. Any attempt to loosen a window
(alert later / narrower net) is rejected. The watcher never quietly
gives you less warning.

Receipts: every ingest proposal, merge, radar run and gap run appends
a hash-chained receipt to the store — append-only, verifiable.

Transport: MCP over stdio (newline-delimited JSON-RPC 2.0), stdlib only.
State file: $ECLIPSE_BEACON_STATE or ./data/state.json beside this file.
"""
import hashlib
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

STATE_PATH = Path(os.environ.get(
    "ECLIPSE_BEACON_STATE",
    Path(__file__).parent / "data" / "state.json"))

# Default alert windows (days of lead time). A window is the number of
# days BEFORE an event at which the radar starts reporting it.
DEFAULT_WINDOWS = {"renewal": 60, "premium_due": 14, "grace_end": 30,
                   "escrow_proof": 30, "enrollment": 30}

SPIKE_THRESHOLD_DEFAULT = 15.0  # percent, premium vs prior term

# Static enrollment calendar — dates MOVE per year/program; flagged
# [verify] on every surface that shows them (spec §4).
ENROLLMENT_WINDOWS = [
    {"name": "ACA Open Enrollment", "applies_to": "health",
     "start": "2026-11-01", "end": "2027-01-15", "verify": True},
    {"name": "Medicare Annual Enrollment Period", "applies_to": "health",
     "start": "2026-10-15", "end": "2026-12-07", "verify": True},
]

TEXT_LABELS = {  # text dec-page label -> normalized field path
    "Policy Type": "policy.policy_type", "Carrier": "policy.carrier",
    "Policy Number": "policy.policy_number",
    "Named Insured": "policy.holder", "Status": "policy.status",
    "Premium Amount": "premium.amount", "Premium Period": "premium.period",
    "Prior Premium Amount": "premium.prior_amount",
    "Effective Date": "dates.effective", "Renewal Date": "dates.renewal",
    "Premium Due Date": "dates.premium_due",
    "Grace Period End": "dates.grace_end",
    "Escrow Proof Deadline": "dates.escrow_proof_deadline",
}
DATE_KEYS = {"effective", "renewal", "premium_due", "grace_end",
             "escrow_proof_deadline"}
POLICY_EVENT_KINDS = [("renewal", "renewal", "Policy renews — coverage continues only if the renewal terms are accepted/paid."),
                      ("premium_due", "premium_due", "Premium due — unpaid premiums start the grace clock."),
                      ("grace_end", "grace_end", "Grace period ends — coverage lapses if the premium is still unpaid."),
                      ("escrow_proof_deadline", "escrow_proof", "Escrow/lender proof deadline — the lender must hold proof of insurance by this date.")]


class ToolError(Exception):
    pass


def now() -> int:
    return int(time.time())


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def parse_date(s):
    if not s:
        return None
    return date.fromisoformat(str(s)[:10])


# --------------------------------------------------------------------- state
def empty_state() -> dict:
    return {"policies": {}, "assets": {}, "persons": {},
            "windows": dict(DEFAULT_WINDOWS), "receipts": []}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return empty_state()


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=1))


# ------------------------------------------------------------------ receipts
def emit_receipt(state: dict, rtype: str, detail: dict) -> dict:
    """Append-only, hash-chained (house pattern)."""
    prev = state["receipts"][-1]["hash"] if state["receipts"] else "GENESIS"
    entry = {"seq": len(state["receipts"]) + 1, "ts": now(), "type": rtype,
             "prev_hash": prev, "detail": detail}
    entry["hash"] = sha(prev + canon({k: v for k, v in entry.items()
                                      if k != "hash"}))
    state["receipts"].append(entry)
    return entry


def verify_receipt_chain(receipts) -> bool:
    prev = "GENESIS"
    for e in receipts:
        if e.get("prev_hash") != prev:
            return False
        expect = sha(prev + canon({k: v for k, v in e.items() if k != "hash"}))
        if e.get("hash") != expect:
            return False
        prev = e["hash"]
    return True


# ----------------------------------------------------------------- vigilance
def validate_windows(proposed: dict, current: dict) -> dict:
    """VIGIL RULE: a window may only be TIGHTENED — its lead time may
    grow (alert earlier), never shrink (alert later). Returns the merged
    windows or raises ToolError naming every loosening attempt."""
    merged = dict(current)
    loosened = []
    for kind, days in (proposed or {}).items():
        if kind not in DEFAULT_WINDOWS:
            raise ToolError(f"unknown alert window: {kind}")
        days = int(days)
        if days < current[kind]:
            loosened.append(f"{kind}: {current[kind]}d -> {days}d")
        else:
            merged[kind] = days
    if loosened:
        raise ToolError(
            "Vigil rule: alert windows may only be tightened (longer lead "
            "time = earlier warning), never loosened. Rejected: "
            + "; ".join(loosened))
    return merged


# -------------------------------------------------------------------- ingest
def _num(value):
    if value is None or (isinstance(value, str)
                          and value.strip().lower() in ("", "none", "n/a")):
        return None
    return float(str(value).replace(",", "").replace("$", ""))


def extract_json(doc: dict, source: str):
    """Returns (draft_policy, provenance, household) — household set only
    for doc_type=household_profile."""
    prov = {}

    def take(path, value, locator):
        if value is not None:
            prov[path] = {"source": source, "locator": locator,
                          "confidence": 1.0}
        return value

    if doc.get("doc_type") == "household_profile":
        household = {"assets": doc.get("assets", []),
                     "persons": doc.get("persons", [])}
        for a in household["assets"]:
            prov[f"asset:{a['asset_id']}"] = {
                "source": source, "locator": "json:$.assets", "confidence": 1.0}
        return None, prov, household

    p, pr, d = doc.get("policy", {}), doc.get("premium", {}), doc.get("dates", {})
    draft = {
        "policy_type": take("policy_type", p.get("policy_type"), "json:$.policy.policy_type"),
        "carrier": take("carrier", p.get("carrier"), "json:$.policy.carrier"),
        "policy_number": take("policy_number", p.get("policy_number"), "json:$.policy.policy_number"),
        "holder": take("holder", p.get("holder"), "json:$.policy.holder"),
        "status": take("status", p.get("status", "active"), "json:$.policy.status"),
        "covered_persons": take("covered_persons", doc.get("covered_persons", []), "json:$.covered_persons"),
        "covered_assets": take("covered_assets", doc.get("covered_assets", []), "json:$.covered_assets"),
        "coverages": take("coverages", doc.get("coverages", []), "json:$.coverages"),
        "premium": {"amount": take("premium.amount", pr.get("amount"), "json:$.premium.amount"),
                    "period": take("premium.period", pr.get("period"), "json:$.premium.period"),
                    "prior_amount": take("premium.prior_amount", pr.get("prior_amount"), "json:$.premium.prior_amount")},
        "dates": {k: take(f"dates.{k}", d.get(k), f"json:$.dates.{k}")
                  for k in sorted(DATE_KEYS)},
    }
    return draft, prov, None


def extract_text(text: str, source: str):
    """Parse a simple dec page: '== SECTION ==' headers + 'Label: value'
    lines + 'Coverage: name | Limit: n | Deductible: n' rows."""
    prov, flat = {}, {}
    covered_persons, covered_assets, coverages = [], [], []
    section = ""
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        m = re.match(r"^==\s*(.+?)\s*==$", line)
        if m:
            section = m.group(1)
            continue
        if line.startswith("Coverage:"):
            parts = [x.strip() for x in line.split("|")]
            cov = {"name": parts[0].split(":", 1)[1].strip(), "limit": None,
                   "deductible": None}
            for part in parts[1:]:
                k, _, v = part.partition(":")
                if k.strip().lower() == "limit":
                    cov["limit"] = _num(v)
                elif k.strip().lower() == "deductible":
                    cov["deductible"] = _num(v)
            coverages.append(cov)
            prov[f"coverages[{len(coverages) - 1}]"] = {
                "source": source,
                "locator": f"text:section '{section}' line {lineno}",
                "confidence": 0.9}
            continue
        if line.startswith("Covered Person:"):
            covered_persons.append(line.split(":", 1)[1].strip())
            continue
        if line.startswith("Covered Asset:"):
            covered_assets.append(line.split(":", 1)[1].strip())
            continue
        label, sep, value = line.partition(":")
        if sep and label.strip() in TEXT_LABELS:
            path = TEXT_LABELS[label.strip()]
            flat[path] = value.strip()
            prov[path] = {"source": source,
                          "locator": f"text:section '{section}' line {lineno}",
                          "confidence": 0.9}
    draft = {
        "policy_type": flat.get("policy.policy_type"),
        "carrier": flat.get("policy.carrier"),
        "policy_number": flat.get("policy.policy_number"),
        "holder": flat.get("policy.holder"),
        "status": flat.get("policy.status", "active"),
        "covered_persons": covered_persons, "covered_assets": covered_assets,
        "coverages": coverages,
        "premium": {"amount": _num(flat.get("premium.amount")),
                    "period": flat.get("premium.period"),
                    "prior_amount": _num(flat.get("premium.prior_amount"))},
        "dates": {k: flat.get(f"dates.{k}") for k in sorted(DATE_KEYS)},
    }
    if covered_persons:
        prov["covered_persons"] = {"source": source, "locator": "text:COVERED",
                                    "confidence": 0.9}
    if covered_assets:
        prov["covered_assets"] = {"source": source, "locator": "text:COVERED",
                                  "confidence": 0.9}
    return draft, prov, None


def extract_document(path: Path):
    if not path.exists():
        raise ToolError(f"file not found: {path}")
    text = path.read_text()
    if path.suffix.lower() == ".json":
        return extract_json(json.loads(text), path.name)
    return extract_text(text, path.name)


def policy_id_for(draft: dict) -> str:
    key = f"{draft.get('carrier', '')}|{draft.get('policy_number', '')}"
    return "pol_" + sha(key)[:10]


def diff_draft(state: dict, draft: dict, prov: dict):
    """Compare a draft against the graph. Returns (policy_id, changes)."""
    pid = policy_id_for(draft)
    existing = state["policies"].get(pid)
    if not existing:
        return pid, [{"field": "*", "change": "new_policy", "to": draft}]
    changes = []
    for field in ("policy_type", "carrier", "policy_number", "holder",
                  "status", "covered_persons", "covered_assets", "coverages"):
        if existing.get(field) != draft.get(field):
            changes.append({"field": field, "from": existing.get(field),
                            "to": draft.get(field)})
    for sub in ("premium", "dates"):
        for k, v in draft.get(sub, {}).items():
            if existing.get(sub, {}).get(k) != v:
                changes.append({"field": f"{sub}.{k}",
                                "from": existing.get(sub, {}).get(k), "to": v})
    return pid, changes


# --------------------------------------------------------------------- tools
def t_policy_ingest(state, args):
    path = Path(str(args["path"])).expanduser()
    confirm = bool(args.get("confirm", False))
    draft, prov, household = extract_document(path)

    if household is not None:
        changes = [{"field": f"asset:{a['asset_id']}", "change": "declare_asset",
                    "to": a} for a in household["assets"]
                   if a["asset_id"] not in state["assets"]]
        changes += [{"field": f"person:{p['person_id']}", "change": "declare_person",
                     "to": p} for p in household["persons"]
                    if p["person_id"] not in state["persons"]]
        emit_receipt(state, "ingest_proposed" if not confirm else "ingest_merged",
                     {"source": path.name, "kind": "household_profile",
                      "changes": len(changes), "confirmed": confirm})
        if not confirm:
            return {"status": "proposed", "confirmed": False,
                    "note": "Propose-only: nothing was written. Re-run with confirm=true to merge.",
                    "changes": changes, "provenance": prov}
        for a in household["assets"]:
            state["assets"][a["asset_id"]] = {**a, "provenance": prov.get(f"asset:{a['asset_id']}")}
        for p in household["persons"]:
            state["persons"][p["person_id"]] = p
        return {"status": "merged", "confirmed": True,
                "assets": sorted(state["assets"]), "persons": sorted(state["persons"])}

    if not draft.get("policy_number") or not draft.get("carrier"):
        raise ToolError("could not extract carrier + policy_number — refusing to draft a policy without identity")
    pid, changes = diff_draft(state, draft, prov)
    emit_receipt(state, "ingest_proposed" if not confirm else "ingest_merged",
                 {"source": path.name, "policy_id": pid,
                  "changes": len(changes), "confirmed": confirm})
    if not confirm:
        return {"status": "proposed", "confirmed": False, "policy_id": pid,
                "note": "Propose-only: nothing was written. Re-run with confirm=true to merge.",
                "draft": draft, "changes": changes, "provenance": prov}
    if changes:
        record = dict(draft)
        record["policy_id"] = pid
        record["provenance"] = prov
        state["policies"][pid] = record
    return {"status": "merged", "confirmed": True, "policy_id": pid,
            "changes_applied": len(changes)}


def t_policy_list(state, args):
    out = []
    for pid, p in sorted(state["policies"].items()):
        out.append({"policy_id": pid, "policy_type": p["policy_type"],
                    "carrier": p["carrier"], "policy_number": p["policy_number"],
                    "status": p["status"],
                    "premium_amount": p["premium"]["amount"],
                    "premium_period": p["premium"]["period"],
                    "renewal": p["dates"].get("renewal"),
                    "covered_assets": p["covered_assets"],
                    "covered_persons": p["covered_persons"]})
    return {"policies": out, "count": len(out),
            "assets": state["assets"], "persons": state["persons"],
            "alert_windows_days": state["windows"]}


def t_policy_get(state, args):
    pid = args["policy_id"]
    p = state["policies"].get(pid)
    if not p:
        raise ToolError(f"unknown policy: {pid}")
    return p


def severity_for(days_until: int) -> str:
    if days_until < 0:
        return "critical"
    if days_until <= 7:
        return "high"
    if days_until <= 30:
        return "medium"
    return "low"


SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def collect_events(state: dict, as_of: date, windows: dict):
    events = []
    for pid, p in state["policies"].items():
        if p.get("status") != "active":
            continue
        for date_key, kind, consequence in POLICY_EVENT_KINDS:
            d = parse_date(p["dates"].get(date_key))
            if d is None:
                continue
            days = (d - as_of).days
            if days <= windows[kind]:
                events.append({
                    "kind": kind, "date": d.isoformat(), "days_until": days,
                    "severity": severity_for(days), "policy_id": pid,
                    "policy_type": p["policy_type"], "carrier": p["carrier"],
                    "consequence": consequence,
                    "provenance": p.get("provenance", {}).get(f"dates.{date_key}")})
    health = [p for p in state["policies"].values()
              if p["policy_type"] == "health" and p.get("status") == "active"]
    if health:
        for w in ENROLLMENT_WINDOWS:
            start, end = parse_date(w["start"]), parse_date(w["end"])
            days = (start - as_of).days
            if -((end - start).days) <= days <= windows["enrollment"]:
                in_window = start <= as_of <= end
                events.append({
                    "kind": "enrollment_window", "date": w["start"],
                    "days_until": days,
                    "severity": "high" if in_window else severity_for(days),
                    "policy_id": None, "policy_type": "health",
                    "carrier": None,
                    "consequence": (f"{w['name']} {'is OPEN' if in_window else 'opens'} "
                                    f"{w['start']} to {w['end']} — plan changes are only "
                                    "possible inside the window. [verify dates per year/program]"),
                    "verify": True})
    events.sort(key=lambda e: (e["days_until"], SEV_RANK[e["severity"]],
                               e["kind"]))
    return events


def t_radar_report(state, args):
    as_of = parse_date(args.get("as_of")) or date.today()
    windows = validate_windows(args.get("windows"), state["windows"])
    events = collect_events(state, as_of, windows)
    r = emit_receipt(state, "radar_report",
                     {"as_of": as_of.isoformat(), "events": len(events),
                      "windows": windows})
    return {"as_of": as_of.isoformat(), "windows_days": windows,
            "events": events, "count": len(events), "receipt_seq": r["seq"],
            "note": "Read/watch only. Beacon signals; it does not bind, pay, or advise."}


def t_gap_report(state, args):
    threshold = float(args.get("spike_threshold_pct", SPIKE_THRESHOLD_DEFAULT))
    findings = []
    coverage_of = {}
    for pid, p in state["policies"].items():
        if p.get("status") != "active":
            continue
        for asset in p["covered_assets"]:
            coverage_of.setdefault(asset, []).append(pid)
    for asset_id, asset in sorted(state["assets"].items()):
        if asset_id not in coverage_of:
            findings.append({"kind": "uncovered_asset", "asset_id": asset_id,
                             "asset": asset,
                             "evidence": "Declared in the household profile; no active policy lists it as covered.",
                             "suggested_next_step": "Owner review: confirm whether coverage exists outside the graph or is genuinely absent. Beacon suggests; it never buys."})
    for asset_id, pids in sorted(coverage_of.items()):
        if len(pids) > 1:
            findings.append({"kind": "duplicate_coverage", "asset_id": asset_id,
                             "policy_ids": pids,
                             "evidence": f"{len(pids)} active policies list this asset as covered.",
                             "suggested_next_step": "Owner review: duplicates can be legitimate (coordination of benefits) or wasted premium."})
    for pid, p in sorted(state["policies"].items()):
        amt, prior = p["premium"].get("amount"), p["premium"].get("prior_amount")
        if amt and prior:
            pct = (amt - prior) / prior * 100.0
            if pct > threshold:
                findings.append({"kind": "premium_spike", "policy_id": pid,
                                 "policy_type": p["policy_type"],
                                 "carrier": p["carrier"],
                                 "change_pct": round(pct, 2),
                                 "threshold_pct": threshold,
                                 "evidence": f"Premium {prior} -> {amt} per {p['premium']['period']} (+{pct:.1f}%). A spike is a signal, not a verdict.",
                                 "suggested_next_step": "Owner review before renewal; B0 prepares nothing and shops nothing."})
    r = emit_receipt(state, "gap_report",
                     {"findings": len(findings), "threshold_pct": threshold})
    return {"findings": findings, "count": len(findings), "receipt_seq": r["seq"],
            "note": "Read-only analysis. Findings are signals with evidence, never verdicts or advice."}


TOOLS = {
    "policy_ingest": (t_policy_ingest, "Ingest a declarations-page-style document (JSON or simple text) from a LOCAL file path. Returns a draft + diff with per-field provenance. DEFAULT IS PROPOSE-ONLY: nothing is written unless confirm=true. Also ingests household_profile JSON (asset/person declarations).",
                      {"path": {"type": "string"}, "confirm": {"type": "boolean"}}, ["path"]),
    "policy_list": (t_policy_list, "List every policy in the household graph with key dates, plus declared assets/persons and current alert windows.",
                    {}, []),
    "policy_get": (t_policy_get, "One policy in full, including per-field provenance.",
                   {"policy_id": {"type": "string"}}, ["policy_id"]),
    "radar_report": (t_radar_report, "Expiry radar: ranked upcoming events (renewal <=60d, premium due <=14d, grace end, escrow proof, enrollment windows [verify]) with severity and consequence. Optional as_of (YYYY-MM-DD) and windows overrides — windows may only be TIGHTENED (longer lead), never loosened (Vigil rule).",
                     {"as_of": {"type": "string"}, "windows": {"type": "object"}}, []),
    "gap_report": (t_gap_report, "Read-only gap analysis: uncovered assets, duplicate coverage, premium spikes vs prior term (default flag >15%).",
                   {"spike_threshold_pct": {"type": "number"}}, []),
}


def tool_list():
    return [{"name": name, "description": desc,
             "inputSchema": {"type": "object", "properties": props, "required": req}}
            for name, (_, desc, props, req) in TOOLS.items()]


def handle(req: dict):
    method, rid = req.get("method", ""), req.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": req.get("params", {}).get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "eclipse-beacon", "version": "0.1.0"}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": tool_list()}}
    if method == "tools/call":
        name = req["params"]["name"]
        args = req["params"].get("arguments", {})
        if name not in TOOLS:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        state = load_state()
        try:
            out = TOOLS[name][0](state, args)
            save_state(state)
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps(out, indent=1)}]}}
        except ToolError as e:
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": str(e)})}], "isError": True}}
    if rid is not None:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None  # notification


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as e:  # never crash the transport
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()

# Eclipse Beacon MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
