#!/usr/bin/env python3
# Eclipse Beacon MCP
# Copyright © 2026 Cyan Eclipse Technologies LLC · cetech.ai
# Eclipse Beacon v0.1 (B0) — unit tests. Run: python3 -m unittest discover tests

"""Eclipse Beacon v0.1 (B0) — unit tests. Run: python3 -m unittest discover tests"""
import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import server as S  # noqa: E402

ROOT = Path(__file__).parent.parent
FIX = ROOT / "fixtures"
AS_OF = "2026-10-25"


def loaded_state():
    st = S.empty_state()
    for name in ("household.json", "auto.json", "homeowners.json",
                 "health.json", "term-life.json"):
        S.t_policy_ingest(st, {"path": str(FIX / name), "confirm": True})
    return st


class TestIngest(unittest.TestCase):
    def test_provenance_completeness(self):
        st = S.empty_state()
        out = S.t_policy_ingest(st, {"path": str(FIX / "auto.json")})
        draft, prov = out["draft"], out["provenance"]
        # every leaf field of the draft has a provenance entry
        for path in ("policy_type", "carrier", "policy_number", "holder",
                     "status", "covered_persons", "covered_assets",
                     "coverages", "premium.amount", "premium.period",
                     "premium.prior_amount", "dates.effective",
                     "dates.renewal", "dates.premium_due", "dates.grace_end"):
            self.assertIn(path, prov, f"missing provenance: {path}")
            self.assertEqual(prov[path]["source"], "auto.json")
            self.assertIn("locator", prov[path])
            self.assertGreater(prov[path]["confidence"], 0)
        self.assertEqual(draft["policy_number"], "PAP-2026-1042")

    def test_text_and_json_parity(self):
        st = S.empty_state()
        j = S.t_policy_ingest(st, {"path": str(FIX / "auto.json")})["draft"]
        t = S.t_policy_ingest(st, {"path": str(FIX / "auto.txt")})["draft"]
        self.assertEqual(j, t)

    def test_merge_without_confirm_writes_nothing(self):
        st = S.empty_state()
        before = copy.deepcopy({k: st[k] for k in ("policies", "assets", "persons")})
        out = S.t_policy_ingest(st, {"path": str(FIX / "auto.json")})
        self.assertEqual(out["status"], "proposed")
        self.assertFalse(out["confirmed"])
        self.assertEqual({k: st[k] for k in ("policies", "assets", "persons")}, before)
        self.assertEqual(st["policies"], {})  # graph untouched
        # ...but the proposal itself is receipted (audit of the attempt)
        self.assertEqual(st["receipts"][-1]["type"], "ingest_proposed")

    def test_merge_with_confirm_and_idempotent_reingest(self):
        st = loaded_state()
        self.assertEqual(len(st["policies"]), 4)
        self.assertIn("boat-1", st["assets"])
        out = S.t_policy_ingest(st, {"path": str(FIX / "auto.json"),
                                     "confirm": True})
        self.assertEqual(out["changes_applied"], 0)  # no drift, no rewrite

    def test_household_propose_only(self):
        st = S.empty_state()
        out = S.t_policy_ingest(st, {"path": str(FIX / "household.json")})
        self.assertEqual(out["status"], "proposed")
        self.assertEqual(st["assets"], {})


class TestRadar(unittest.TestCase):
    def setUp(self):
        self.st = loaded_state()

    def test_ordering_and_membership(self):
        out = S.t_radar_report(self.st, {"as_of": AS_OF})
        days = [e["days_until"] for e in out["events"]]
        self.assertEqual(days, sorted(days))  # ranked ascending
        kinds = {(e["kind"], e["policy_type"]) for e in out["events"]}
        self.assertIn(("premium_due", "health"), kinds)      # 7d
        self.assertIn(("premium_due", "auto"), kinds)        # 11d
        self.assertIn(("premium_due", "term_life"), kinds)   # 14d
        self.assertIn(("escrow_proof", "homeowners"), kinds)  # 26d
        self.assertIn(("grace_end", "auto"), kinds)          # 26d
        self.assertIn(("renewal", "homeowners"), kinds)      # 51d
        # outside default windows -> excluded
        self.assertNotIn(("renewal", "auto"), kinds)         # 127d > 60
        self.assertNotIn(("premium_due", "homeowners"), kinds)  # 37d > 14

    def test_severity(self):
        out = S.t_radar_report(self.st, {"as_of": AS_OF})
        by = {(e["kind"], e["policy_type"]): e for e in out["events"]}
        self.assertEqual(by[("premium_due", "health")]["severity"], "high")
        self.assertEqual(by[("renewal", "homeowners")]["severity"], "low")
        overdue = S.t_radar_report(self.st, {"as_of": "2026-11-06"})
        auto_due = [e for e in overdue["events"]
                    if e["kind"] == "premium_due" and e["policy_type"] == "auto"]
        self.assertEqual(auto_due[0]["severity"], "critical")
        self.assertEqual(auto_due[0]["days_until"], -1)

    def test_enrollment_windows_flagged_verify(self):
        out = S.t_radar_report(self.st, {"as_of": AS_OF})
        enr = [e for e in out["events"] if e["kind"] == "enrollment_window"]
        self.assertTrue(enr)
        for e in enr:
            self.assertTrue(e["verify"])
            self.assertIn("[verify", e["consequence"])

    def test_event_provenance_attached(self):
        out = S.t_radar_report(self.st, {"as_of": AS_OF})
        pol_events = [e for e in out["events"] if e["policy_id"]]
        for e in pol_events:
            self.assertIsNotNone(e["provenance"])
            self.assertIn("source", e["provenance"])


class TestVigilRule(unittest.TestCase):
    def test_tightening_accepted(self):
        merged = S.validate_windows({"renewal": 90}, dict(S.DEFAULT_WINDOWS))
        self.assertEqual(merged["renewal"], 90)

    def test_loosening_rejected(self):
        with self.assertRaises(S.ToolError) as ctx:
            S.validate_windows({"premium_due": 7}, dict(S.DEFAULT_WINDOWS))
        self.assertIn("Vigil rule", str(ctx.exception))

    def test_loosening_rejected_via_tool(self):
        st = loaded_state()
        with self.assertRaises(S.ToolError):
            S.t_radar_report(st, {"as_of": AS_OF,
                                  "windows": {"grace_end": 10}})


class TestGaps(unittest.TestCase):
    def setUp(self):
        self.st = loaded_state()

    def test_uncovered_asset(self):
        out = S.t_gap_report(self.st, {})
        unc = [f for f in out["findings"] if f["kind"] == "uncovered_asset"]
        self.assertEqual([f["asset_id"] for f in unc], ["boat-1"])

    def test_premium_spike(self):
        out = S.t_gap_report(self.st, {})
        spikes = [f for f in out["findings"] if f["kind"] == "premium_spike"]
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["carrier"], "Cascade Mutual (fictional)")
        self.assertAlmostEqual(spikes[0]["change_pct"], 21.19, places=2)
        # homeowners +4.96% and health +4.04% stay under the 15% flag
        out2 = S.t_gap_report(self.st, {"spike_threshold_pct": 4.0})
        self.assertEqual(len([f for f in out2["findings"]
                              if f["kind"] == "premium_spike"]), 3)

    def test_duplicate_coverage(self):
        # second (synthetic variant) policy covering vehicle-1
        doc = json.loads((FIX / "auto.json").read_text())
        doc["policy"]["policy_number"] = "PAP-2026-9999"
        doc["policy"]["carrier"] = "Second Shield Casualty (fictional)"
        tmp = Path(self.id().replace(".", "_") + ".json")
        try:
            tmp.write_text(json.dumps(doc))
            S.t_policy_ingest(self.st, {"path": str(tmp.resolve()),
                                        "confirm": True})
        finally:
            tmp.unlink(missing_ok=True)
        out = S.t_gap_report(self.st, {})
        dup = [f for f in out["findings"] if f["kind"] == "duplicate_coverage"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["asset_id"], "vehicle-1")
        self.assertEqual(len(dup[0]["policy_ids"]), 2)


class TestReceipts(unittest.TestCase):
    def test_chain_valid_and_append_only(self):
        st = loaded_state()
        S.t_radar_report(st, {"as_of": AS_OF})
        S.t_gap_report(st, {})
        self.assertTrue(S.verify_receipt_chain(st["receipts"]))
        seqs = [r["seq"] for r in st["receipts"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        types = [r["type"] for r in st["receipts"]]
        self.assertIn("ingest_merged", types)
        self.assertIn("radar_report", types)
        self.assertIn("gap_report", types)
        # tampering breaks the chain
        st["receipts"][0]["detail"]["changes"] = 999
        self.assertFalse(S.verify_receipt_chain(st["receipts"]))


class TestTransport(unittest.TestCase):
    def test_tools_list_and_call(self):
        resp = S.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [t["name"] for t in resp["result"]["tools"]]
        self.assertEqual(names, ["policy_ingest", "policy_list", "policy_get",
                                 "radar_report", "gap_report"])
        resp = S.handle({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                         "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(resp["result"]["serverInfo"]["name"], "eclipse-beacon")


if __name__ == "__main__":
    unittest.main()

# Eclipse Beacon MCP · © 2026 Cyan Eclipse Technologies LLC · cetech.ai
