# eclipse-beacon

mcp-name: io.github.CYAN-ECLIPSE-TECHNOLOGIES-LLC/beacon

**What this is:** `eclipse-beacon` is the MCP server edition of
**Eclipse Beacon** — Cyan Eclipse Technologies' insurance watcher: it
watches your household's policies, signals expiries before they
happen, and prepares the actions you approve.

**What this is not:** it does **not** insure, bind, cancel, pay, or
advise. Beacon's personal edition is read/watch only, and acts only on
the owner's own household data.

## Use

```console
$ pip install eclipse-beacon
$ eclipse-beacon            # MCP server over stdio
```

Or run it as a container: `ghcr.io/cyan-eclipse-technologies-llc/eclipse-beacon-mcp:0.1.0`.

State lives in `data/state.json` beside the server (override with
`ECLIPSE_BEACON_STATE`). Python stdlib only; **no network calls
anywhere in the code**.

## Tools (5)

| Tool | What it does |
|---|---|
| `policy_ingest` | Parse a declarations-page-style document (JSON or simple text) from a local file. Returns a draft + diff with per-field provenance. Propose-only unless `confirm=true`. |
| `policy_list` | Every policy in the graph with key dates, plus declared assets/persons and current alert windows. |
| `policy_get` | One policy in full, provenance included. |
| `radar_report` | Ranked upcoming events (renewal ≤60d, premium due ≤14d, grace-period end, proof deadlines, enrollment calendar) with severity, days-until, and the consequence of missing each. |
| `gap_report` | Read-only analysis: uncovered assets, duplicate coverage, premium spikes. Findings are signals with evidence — never verdicts, never advice. |

## Honest scope

- **v0.1 is phase B0 only** (personal edition: policy graph + expiry
  radar). Renewal shopping, claims, binding, and the agency edition
  are explicitly out of scope.
- The shipped fixtures are **synthetic** — fictional carriers,
  synthetic people, invented policy numbers.
- **The Vigil rule:** alert windows may only be *tightened* (warn
  earlier), never loosened. The watcher never quietly gives you less
  warning.

Powered by Cyan Eclipse Technologies LLC (cetech.ai) — a Veteran-Owned
Small Business. MIT licensed.
