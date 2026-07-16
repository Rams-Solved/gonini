# gonini

**Why this exists**

I built this small tool in an evening to show my understanding of the role, not just claim it. It models the daily reconciliation problem described in the job spec: three systems holding different versions of order truth, a deterministic engine that diffs and classifies the exceptions, and an LLM fenced to narration and drafting only. Nothing is sent without human approval.
This repo is linked in my application.
Below are two screenshots of the tool in action, including a mistake the LLM made in a root-cause narrative. I have kept it in deliberately: the deterministic owner column beside it is correct, which is exactly why the numbers never come from the model. That boundary is the point of the design.

<img width="1465" height="881" alt="Gonini Demo 1" src="https://github.com/user-attachments/assets/33089823-84e4-40d1-b7c3-7bdaa6e2fb60" />
<img width="1097" height="558" alt="Gonini Demo 2" src="https://github.com/user-attachments/assets/4a7ab509-65d6-489c-91b1-db3109876743" />


**An order exception reconciliation agent for a Fulfilment-as-a-Service platform.**

Every morning a FaaS operation wakes up to three systems that each believe a
different version of the truth: the **Platform** (what the seller ordered), the
**WMS** (what the warehouse did), and the **Carrier** (what actually moved).
Add the **invoice** and the **rate card** and you have five sources that
drift apart daily. `gonini` reconciles them, flags every divergence into a
fixed taxonomy, prioritises the mess, and drafts the escalation emails — then
stops and waits for a human.

The core principle: **a deterministic rules engine does all the diffing,
flagging, and pricing** (auditable, reproducible, zero hallucination surface).
**An LLM is fenced into a narrow lane** — classifying ambiguous prose, inferring
root causes, prioritising the narrative, and drafting comms — and it never sees
or produces the numbers. All outbound communication is drafted to an `/outbox`
folder for approval; nothing is ever sent.

---

## What runs unattended, and where the human sits

This is the whole design, so it goes first.

```
UNATTENDED (safe to run on a cron, no human in the loop)
──────────────────────────────────────────────────────────────────────
  1. Pull the five data sets            gonini seed        (demo: mock CSVs)
  2. Diff + classify + price + score    gonini reconcile   (deterministic)
  3. Assemble the digest                gonini digest      (rules + fenced LLM)
  4. DRAFT escalation emails to /outbox                    (never sent)
══════════════════════ ✋ HUMAN APPROVAL GATE ══════════════════════════
  5. A person reads the one-page digest, opens the /outbox drafts,
     edits/approves, and is the one who actually clicks send.
──────────────────────────────────────────────────────────────────────
```

**What is safe to automate** is everything that is *deterministic and
reversible*: reading data, computing diffs, writing a report, and preparing
draft text. None of it changes the outside world.

**Where the human sits** is at every point where an action leaves the building
— sending an email to a carrier, raising a billing dispute, crediting a seller.
`gonini` produces the artefact and the evidence; a person makes the call. The
`/outbox` folder is the seam: it fills up with `DRAFT — PENDING HUMAN APPROVAL`
markdown files, and that is the last thing the agent does. It has no `send`.

Concretely, in this repo:

- `gonini reconcile` writes findings to SQLite. No side effects beyond the DB.
- `gonini digest` writes a digest and one `.md` draft per responsible party to
  `/outbox`. Each is explicitly marked as a draft. **There is no send path in
  the codebase** — not disabled, not stubbed to a no-op, simply absent.
- The LLM, when enabled, is instructed it is drafting for human approval and is
  forbidden from inventing or altering any number.

---

## Quick start

No dependencies, no API key. Requires Python 3.11+.

```bash
# End to end: seed → reconcile → digest, prints the digest
python -m gonini demo --no-llm

# …or step by step
python -m gonini seed        # generate mock CSVs in /data
python -m gonini reconcile   # deterministic diff → SQLite
python -m gonini digest --no-llm   # digest + /outbox drafts
```

Or install the console script:

```bash
pip install -e .            # provides the `gonini` command
gonini demo --no-llm
```

**LLM mode.** Drop `--no-llm` and set an API key to have a real model write
the narrative, root causes, and email prose instead of the offline templates.
See [Providers](#providers) below for which key picks which backend. Without
any key, `gonini` auto-falls back to the offline templates and says so in the
digest header, so the demo always runs.

Outputs land in:

- `data/*.csv` — the mock systems of record (Platform, WMS, Carrier, rate card, invoice)
- `data/gonini.db` — the persisted exception table + SLA + recovery summaries
- `outbox/digest_<date>.md` — the one-page morning digest
- `outbox/DRAFT_<party>_<date>.md` — one escalation draft per responsible party

---

## Providers

`gonini` talks to a real model through two interchangeable backends behind the
same `LLMClient` interface (`llm/base.py`) — same fence, same fallback
behaviour, same digest/email shape either way. Which one runs is resolved in
this order:

1. `--provider anthropic|openrouter|mock` — explicit override, always wins.
2. Auto (the default, `--provider auto` or the flag omitted):
   - `OPENROUTER_API_KEY` set → **OpenRouter**
   - else `ANTHROPIC_API_KEY` set → **Anthropic**
   - else → offline templates (same as `--no-llm`)

`--no-llm` always forces the templates regardless of `--provider`.

| Provider | Env var | Default model | Client |
|---|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-sonnet-5` | Official `anthropic` SDK (optional dep, `pip install -e '.[llm]'`) |
| OpenRouter | `OPENROUTER_API_KEY` | `openrouter/free` | OpenAI-compatible chat completions over stdlib `urllib` — no extra dependency |

`--model` overrides the model id for whichever provider is selected.

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...
gonini digest                                   # auto-selects anthropic

# OpenRouter — openrouter/free auto-routes to a live free model
export OPENROUTER_API_KEY=sk-or-v1-...
gonini digest                                   # auto-selects openrouter (takes priority over Anthropic)
gonini digest --provider openrouter --model mistralai/mistral-small-3.2-24b-instruct:free

# Force a provider or the templates regardless of env vars
gonini digest --provider anthropic
gonini digest --provider mock                   # same as --no-llm
```

**Reliability.** Both providers retry a transient failure (HTTP 429 or 5xx) up
to 3 times with exponential backoff before giving up; if the call still fails
— or fails for any other reason — `gonini` falls back to the offline
templates rather than dying mid-run, and says so in the digest header
(`narrative by mock (auto-fallback: ...)`).

**JSON hardening (OpenRouter).** `openrouter/free` and other free-tier models
are noticeably weaker than Sonnet at returning strict JSON: they'll wrap a
reply in markdown fences or add a sentence of prose around it despite being
told not to. `gonini` strips fences and tolerates surrounding prose when
parsing (`llm/json_utils.py`), and if that still doesn't yield valid JSON, it
retries once with an explicit "JSON only" instruction before giving up and
falling back to the templates. The Anthropic path doesn't need this — it uses
`output_config.format` structured outputs, which constrain the response
directly.

**OpenRouter free-tier limits.** The free-model endpoints are rate-limited to
roughly **50 requests/day** and **20 requests/minute** per API key (per
OpenRouter's published limits at the time of writing — check
[openrouter.ai](https://openrouter.ai) for current figures). A `digest` run
makes one call per responsible party for email drafts plus one for the
headline and one for root causes, so a handful of runs can exhaust the daily
quota; a 429 there is retried automatically and then falls back to templates
rather than failing the run.

---

## Architecture

```
   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────┐
   │  Platform    │   │     WMS       │   │   Carrier    │   │  Invoice  │
   │  orders      │   │   events      │   │   tracking   │   │ + ratecard│
   └──────┬───────┘   └──────┬───────┘   └──────┬───────┘   └─────┬─────┘
          │ (CSV today; API pulls in prod)                        │
          └───────────────┬──────────────────┬───────────────────┘
                          ▼                  ▼
              ┌──────────────────────────────────────────┐
              │   DETERMINISTIC RULES ENGINE (reconcile)  │   ← no model here
              │   join on order_id · diff states/qty/time │
              │   classify → taxonomy · severity · £      │
              │   responsible party · evidence (raw rows) │
              └───────────────────┬──────────────────────┘
                                  ▼
                        ┌───────────────────┐
                        │   SQLite (audit)   │  exceptions · SLA · recovery
                        └─────────┬─────────┘
                                  ▼
              ┌──────────────────────────────────────────┐
              │        FENCED LLM LAYER (digest)          │   ← prose only
              │  headline narrative · root-cause guesses  │
              │  escalation email drafting                │
              │  (interface + --no-llm template fallback) │
              └───────────────────┬──────────────────────┘
                                  ▼
              ┌──────────────────────────────────────────┐
              │  Morning digest (≤1 page)                 │
              │  /outbox/*.md  DRAFT — pending approval   │
              └──────────────────────────────────────────┘
                                  ▼
                            ✋ human approves & sends
```

The LLM sits **downstream** of the numbers. It is handed facts and evidence the
engine already computed and is asked only to phrase them.

---

## Exception taxonomy

Every divergence is classified into exactly one of these ten types. Severity is
rule-based and follows the ordering **seller-facing > money > hygiene**.

| Type | Category | Severity | What it means | Owner |
|---|---|---|---|---|
| `MISSING_IN_WMS` | Seller-facing | Critical | Platform has the order; the WMS never received it | Warehouse intake |
| `STUCK_NO_MOVEMENT` | Seller-facing | High | In the WMS but no movement for > 24h; not despatched | Warehouse ops |
| `DESPATCHED_NOT_SCANNED` | Seller-facing | High | Marked despatched > 24h ago; no carrier scan | Warehouse ops |
| `TRACKING_STALLED` | Seller-facing | High | Carrier tracking with no update for > 72h; not delivered | Carrier |
| `SLA_DESPATCH_BREACH` | Seller-facing | High | Despatched after the ship-by SLA | Warehouse ops |
| `QTY_MISMATCH` | Money | Medium | Quantity despatched ≠ quantity ordered | Warehouse ops |
| `INVOICE_OVERBILL` | Money | High | Billed above the recomputed rate card | Warehouse billing |
| `INVOICE_PHANTOM` | Money | High | Invoice line with no matching order / off rate card | Warehouse billing |
| `INVOICE_DUPLICATE` | Money | High | The same fulfilment billed twice | Warehouse billing |
| `DELIVERED_NOT_CLOSED` | Hygiene | Low | Carrier delivered > 24h ago; platform still open | Platform ops |

Prioritisation is deterministic: sort by category (seller-facing → money →
hygiene), then severity, then largest recoverable amount, then order id. The
digest shows the top ten — signal, not exhaust.

Each exception record carries: `order_id`, `type`, `severity`, the raw
conflicting rows as `evidence`, the `responsible_party` (warehouse / carrier /
platform), and a `recoverable_pence` figure for billing issues.

Alongside the exception table, `reconcile` computes **per-warehouse despatch-SLA
attainment**, **per-carrier delivery-health attainment**, and the **invoice
delta** (expected vs billed, total recoverable £), all persisted to SQLite.

---

## The mock data

`gonini seed` writes five CSVs to `/data` with a fixed seed, so every run is
byte-for-byte reproducible. ~200 orders, ~15% carrying a deliberate anomaly
(three of each taxonomy type).

| File | Columns |
|---|---|
| `platform_orders.csv` | order_id, seller_id, channel, created_at, sku, qty, status, ship_by_sla |
| `wms_events.csv` | order_id, warehouse_id, event, timestamp, qty |
| `carrier_tracking.csv` | order_id, carrier_id, tracking_no, event, timestamp |
| `rate_card.csv` | warehouse_id, service, unit_price |
| `invoice.csv` | warehouse_id, order_id, line_item, qty, amount |

Verisimilitude matters more than volume: timestamps tell coherent stories.
Anomaly timelines are built *backward* from the reconciliation instant so their
"N hours ago" ages are exact, and every order respects
`received < picked < packed < despatched < label < collected < in_transit <
delivered`. An order despatched before it was picked would be embarrassing;
the generator's invariants make it impossible.

---

## From demo to real deployment

The demo fakes two things — where the data comes from, and what pulls the
trigger. Both are swaps at the edges; the engine and taxonomy are unchanged.

**CSVs → API pulls.** `gonini seed` stands in for the daily data pull. In
production, replace it with connectors that page the Platform, WMS, and Carrier
APIs (and the billing export) into the same five shapes. `reconcile._load_sources`
is the single seam — point it at the API responses instead of CSV readers and
nothing downstream changes. SQLite is fine for a demo; a real deployment would
use the platform's warehouse/OLTP store, but the schema in `db.py` is the same.

**Manual run → cron / Make.** The daily job is
`seed → reconcile → digest`. In production, drop the seed step and schedule
`reconcile && digest` — a cron entry, a `Makefile` target invoked by a
scheduler, or an Airflow/Temporal task:

```cron
# 06:30 every weekday: reconcile last night's data, draft the morning digest
30 6 * * 1-5  cd /srv/gonini && gonini reconcile && gonini digest >> /var/log/gonini.log
```

**The `/outbox` → a review queue.** The draft files become items in a review UI
or a shared mailbox; approval (a human clicking send) is the only step that ever
touches the outside world. That gate does not move.

---

## Why the diff is deterministic and the LLM is fenced

The two jobs in this pipeline have opposite failure modes, so they get opposite
tools.

**Diffing is a correctness problem.** "Was this despatched after its SLA?" has
one right answer, it needs to be the same answer every run, and when a carrier
disputes it you must be able to point at the exact rows and the exact rule. A
language model is the wrong tool for this: it is non-deterministic, it cannot be
audited line-by-line, and a confident-but-wrong flag on a real invoice is a real
financial error. So the rules engine is plain Python — every threshold is a
named constant, every flag traces to conflicting rows stored as evidence, and
the same input always yields the same output. You can unit-test it, diff two
runs, and defend a finding.

**Narration is a language problem.** "Write a polite escalation to the DPD
account manager summarising these three stalled parcels" has no single right
answer and benefits enormously from fluency and judgement about ambiguity. That
is exactly what an LLM is good at — so that, and only that, is what it does here.

The fence is enforced structurally, not by good intentions:

- The LLM runs **downstream** of the engine and is handed pre-computed facts and
  evidence. It has no path to the raw diff logic.
- Its interface (`llm/base.py`) exposes only three verbs: narrate, hypothesise a
  root cause, draft an email. There is no "classify" or "compute" method.
- The system prompt forbids inventing or altering any id, amount, quantity,
  timestamp, or count — and because those numbers are rendered by the
  deterministic layer, a model that ignored the instruction still could not
  change what the digest reports.
- Everything the LLM produces is prose in a **draft**, gated behind human
  approval before it can act.

The `--no-llm` mode isn't just a demo convenience — it proves the point. Swap
the model for templates and the numbers, severities, priorities, and recovery
totals are identical. The intelligence changed; the truth didn't.

---

## Project layout

```
gonini/
  config.py       thresholds, reference data, paths, the fixed AS_OF instant
  taxonomy.py     the ten types + category/severity mapping (code-owned)
  models.py       typed records (exceptions, SLA, recovery)
  seed.py         deterministic mock-data generation
  reconcile.py    the deterministic rules engine  ← all diffing lives here
  db.py           SQLite persistence (the audit log)
  digest.py       digest assembly + /outbox drafting
  llm/
    base.py       the fenced LLMClient interface
    mock.py       offline template fallback (--no-llm)
    anthropic_client.py   real Anthropic (claude-sonnet-5) implementation
    openrouter_client.py  real OpenRouter (openrouter/free) implementation
    shared.py     system prompt, schemas, payload shaping shared by both
    retry.py      exponential-backoff retry on 429/5xx, shared by both
    json_utils.py tolerant JSON extraction (fences, stray prose)
  cli.py          seed / reconcile / digest / demo
```

No auth, no web UI, no heavy frameworks — demo-scale, production-shaped.
