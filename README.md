# Eclipse Beacon MCP

**Powered by Cyan Eclipse Technologies** — https://cetech.ai

**Eclipse Beacon — the insurance watcher: it watches your household's policies, signals expiries before they happen, and prepares the actions you approve. It does not insure, bind, or advise.**

## Why this exists

A household's insurance — health, auto, home, life — has no system of record. Policies live as PDFs in email and paper in a drawer, and expiries are usually discovered by a lapse notice. Beacon B0 is the watcher that fixes the first half of that: a local policy graph with provenance on every field, and an expiry radar that signals deadlines *before* they arrive.

## Status — honest labels

v0.1.0, phase **B0 only (personal edition: policy graph + expiry radar)**. **Read/watch only.** Local stdio transport, Python stdlib only, **no network calls anywhere in the code**, and the shipped fixtures are **synthetic** (fictional carriers, synthetic people, invented policy numbers). **No certification claimed — bench pending.**

Explicitly NOT in v0.1, by spec: renewal shopping, renewal prep, claims filing, proof delivery, binding, cancelling, paying — and the entire agency edition, which is gated behind counsel + licensing review. Binding, negotiating, and advising for others are state-licensed producer activities; Beacon's personal edition acts only on the owner's own household data, and B0 doesn't even do that — it watches.

## Tools (5)

| Tool | What it does |
|---|---|
| `policy_ingest` | Parse a declarations-page-style document (JSON, or simple text dec page) from a **local file**. Returns a draft + diff with **per-field provenance** (source file, locator, confidence). **Propose-only by default — nothing is written unless `confirm=true`.** Also ingests `household_profile` JSON (asset/person declarations). |
| `policy_list` | Every policy in the graph with key dates, plus declared assets/persons and current alert windows. |
| `policy_get` | One policy in full, provenance included. |
| `radar_report` | Ranked upcoming events: renewal (≤60d), premium due (≤14d), grace-period end, escrow/lender proof deadline, and static enrollment calendar entries (ACA / Medicare — dates move yearly, flagged **[verify]** on the surface). Each event carries severity, days-until, the consequence of missing it, and the provenance of the date it came from. |
| `gap_report` | Read-only analysis: assets with no covering policy, duplicate coverage on the same asset, premium spikes vs the prior term (default flag >15%). Findings are signals with evidence — never verdicts, never advice. |

## The Vigil rule (encoded + tested)

Alert windows are owner-configurable, but a change may only **tighten** a window — lengthen its lead time so Beacon warns *earlier*. Any attempt to loosen a window (warn later, narrower net) is rejected with the reason named. The watcher never quietly gives you less warning.

## Receipts

Every ingest proposal, merge, radar run and gap run appends a **hash-chained receipt** to the store — append-only and machine-verifiable (`verify_receipt_chain`). Proposals are receipted too: the audit trail covers what was *attempted*, not just what landed.

## Run it

```bash
python3 server.py            # MCP over stdio, stdlib only, no dependencies
python3 smoke.py             # end-to-end smoke over real stdio (temp state)
python3 -m unittest discover tests   # 17 tests
```

State persists in `data/state.json` beside the server (override with `ECLIPSE_BEACON_STATE`). `data/` is gitignored — a real household's graph never belongs in a repo.

### Fixtures

`fixtures/` ships four synthetic policies — auto, homeowners, health, term life — each as an ACORD-shaped JSON dec page **and** an equivalent simple-text dec page (the two parse to identical drafts; a test proves it), plus `household.json` declaring the household's assets and persons — including one boat that no policy covers, so the gap report has something honest to find.

## Roadmap (per spec)

- **B1** — gap watcher expansion + lapse guard (escalation before lapse; still no transactions).
- **B2** — mandate-gated action prep: renewal shopping packs, renewal prep, claims FNOL drafting, proof delivery — owner-approved, none binding.
- **B3** — agency edition: **blocked on counsel + licensing review.** Does not start before the gate clears.

## License / attribution

A Cyan Eclipse Technologies product. The "Powered by Cyan Eclipse Technologies" attribution travels with it.
