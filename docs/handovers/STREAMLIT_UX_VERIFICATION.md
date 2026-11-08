# Streamlit analyst UX verification

8 November 2026 (Asia/Singapore), on `streamlit`, starting from clean commit
`632a679`. This milestone changes presentation and fixes selection state; the
agent, contracts, fixtures, AWS services and infrastructure are unchanged.
The earlier [live verification](STREAMLIT_AWS_VERIFICATION.md) remains historical
and was not repeated. No real AWS or Bedrock calls were made for this milestone.

## What changed

- **Queue:** AML Alert Review introduction, three-step help, compact date/limit
  controls and six primary columns. Streamlit 1.65.0 supports single-row
  dataframe selection; clicking a row updates the selected summary and the
  prominent Open Selected Investigation action. An expandable keyboard selector
  is an alternative, not a required second selection. Sender, receiver, case,
  role and finding codes are secondary details. New sessions start at 20;
  all nine current 14 September Demo rows are retained, in original BQ1 order.
- **Selection fix:** Open uses a guarded lookup in the active source's snapshot.
  A missing/stale ID displays “This alert is no longer in the loaded queue.
  Please select another alert.” It does not navigate, read a saved record or
  discard the queue. Every load creates a new table generation, and row events
  from replaced snapshots are ignored. Invalid IDs are reset before keyed
  widgets render. Date/review-limit defaults restore from the loaded snapshot
  after page navigation; source changes reset controls and selection. Empty
  queues have no transaction selector and a disabled investigation action.
- **Investigation:** compact alert identity/rank/score, supplied summary,
  plain-English finding headings and original finding sentences precede the
  native graph and adjacent evidence card. Grouped cases explain representative
  identity, member selection and overall finding scope. Saved metadata is
  expandable; saved and on-demand views remain independent.
- **Evidence:** clicking a citation selects its exact graph edge and shows
  sender, receiver, amount, currency, time, role, relative minutes and associated
  findings. Trace-only records name the source tool/step and explicitly say they
  are outside the graph. Account-activity endpoints use the supplied account,
  incoming/outgoing collection and counterparty; unknown fields remain missing.
  Other-member, unresolved and capped references retain their existing safe
  behavior. Raw evidence is available only in an optional expander.
- **Graph:** deterministic role-based positions, wrapped larger account labels,
  clear arrowheads, thicker flagged transfer, and a thicker selected edge with a
  text label. Larger graphs place accounts in additional columns rather than one
  unbounded vertical stack; zoom/pan/fit controls remain available. Every payment
  retains its exact ID, endpoints, direction and currency. No legacy transforms,
  aggregation, inferred edges or external graph CDN were introduced.
- **Analysis and execution:** supplied gaps and suggested next checks remain
  readable. Money-flow metrics retain currency-separated totals and a quick
  onward share tooltip; observations remain unverified interpretations. Model
  metadata and raw tool trace stay expandable. New investigation controls are
  below the saved result in a collapsed advanced section, preserving explicit
  acknowledgement, separate execution, request identity and session retention.
- **Navigation and monitoring:** descriptive sidebar labels/help, one prominent
  source notice and collapsed detailed disclosures. Live AWS records are also
  identified as results from the synthetic IBM AML dataset. Real-Time Monitoring
  explains scored/pending/completed records and its separate Lambda pipeline;
  field mappings, timestamp semantics and explicit load/refresh are unchanged.

## Offline browser walkthrough

Chrome ran headlessly against a separate local Streamlit process at
`127.0.0.1:8504`. A temporary launcher outside Git blocked AWS client creation,
SDK dispatch, Bedrock calls and the real investigation entry point, isolated
credential paths and blocked non-loopback socket connections. It did not inject
replay results or replace the app's Demo services. No AWS/agent guard was reached.
Browser-driver scripts and logs remain outside Git under
`/private/tmp/bda-ux-preview/`.

The following were inspected in the browser:

1. The 14 September Demo queue, all nine rows, readable six-column display,
   native row click, selected summary and correct investigation navigation.
2. `txn_samplepass`: original summary and finding; click
   `txn_samplepassdown1` and inspect **US$95,000**, sender
   `200|SAMPLE2`, receiver `900|SAMPLEPASSDOWN1`,
   **14 Sep 2022, 10:25:00 UTC**, **25 minutes after** the flagged transaction,
   finding association and the exact edge labelled Selected, width 6.
3. Switch to `txn_samplepassup`: the card updates to its actual **US$200,000**,
   upstream relationship and **120 minutes before** the flagged transaction.
4. Return to queue, load 16 September, select `txn_samplequiet`, reload
   14 September and open the current valid alert. No StopIteration or Streamlit
   state-mutation exception occurred.
5. `txn_samplefanmember`: grouped-case context; selecting `txn_samplefanup`
   switches to its member. `txn_samplefanout1` shows trace-only account activity,
   actual endpoints and source step without adding a graph edge.
6. `txn_demounprocessed`: friendly missing-saved state, available explicit
   execution controls, no graph fabricated.
7. Real-Time Monitoring Demo: completed explanation and pending/missing brief
   displays, without starting a pipeline or refreshing automatically.

The 1440px desktop view was inspected in both light and dark themes. At 390px,
the native layout stacked the graph above its evidence card; the body width
remained 390px, without page-wide overflow. The graph keeps a light, high-contrast
canvas within either theme; labels and controls remained readable. Mobile zoom
and pan remain useful for long account identifiers.

The screenshots below contain only labelled synthetic Demo records, not raw AWS
responses or screenshots from the earlier live verification.

- [Revised queue](images/streamlit-ux-queue.png)
- [Investigation summary and findings](images/streamlit-ux-investigation.png)
- [Graph and selected supporting transaction](images/streamlit-ux-evidence.png)

## File inventory

| Area | Modified / created files |
|---|---|
| Application navigation | `app.py` |
| Queue and shared source presentation | `src/ui/pages/queue.py`, `src/ui/pages/batch.py`, `src/ui/components/notices.py` |
| Investigation and findings | `src/ui/pages/investigation.py`, `src/ui/components/agent_brief.py` |
| Graph and evidence | `src/ui/components/agent_graph.py`, `src/ui/components/evidence.py`; new `src/ui/components/transaction_detail.py` |
| Real-time presentation | `src/ui/pages/realtime.py`, `src/ui/components/realtime.py` |
| Existing regression updates | `tests/ui/test_agent_demo.py`, `tests/ui/test_batch_dashboard.py`, `tests/ui/test_demo_readiness.py` |
| New regression coverage | `tests/ui/test_analyst_ux.py` |
| Documentation | `docs/streamlit_dashboard.md`, `docs/handovers/STREAMLIT_LIVE_DEMO_RUNBOOK.md`; new `docs/handovers/STREAMLIT_UX_VERIFICATION.md` |
| New screenshots | The three linked PNG files under `docs/handovers/images/` |

## Automated regression checks

The complete unit/UI suite passes **773 tests**, up from 753. Twenty new
parameterised cases in `tests/ui/test_analyst_ux.py` cover date changes in both
directions, Top K removal/reload, Demo → Live selection, explicit Live loading,
invalid Open callbacks/source mismatch, visible stale-selection notices, empty
queues, valid/invalid/stale row events, six-column ordering, supplied summary and
payment-card values, evidence changes with no service reload, trace endpoint/
offset mapping, unknown fields, larger deterministic graphs and the investigation
hierarchy/advanced controls.

Existing coverage was retained for saved-result errors/missing decisions,
other-member citations, unresolved/capped graphs, exact directions/currencies,
independent saved/on-demand results, explicit cost gates, all nine agent example
scenarios, real-time mappings/refresh and the legacy page. Existing expectations
were updated only for intentional labels, columns and sidebar disclosure placement.

The full suite used `/private/tmp/bda-final-offline-check.py`, which blocks real
Botocore API calls and socket connections, removes credential environment
selection and disables metadata lookup. It recorded **zero forbidden external
operation attempts**. UI tests also retain their fail-on-use AWS/agent fixtures.

Validation commands:

```bash
.venv/bin/python -m pytest tests/unit/ tests/ui/ -q
.venv/bin/ruff check app.py src/ui tests/ui
.venv/bin/python -m pip check
git diff --check
```

The test command was executed through the temporary network-blocking launcher.
Modified-file Ruff, dependency consistency and whitespace checks pass.
Repository-wide Ruff retains exactly the same **14 baseline findings**, in files
unchanged by this milestone. No unrelated lint fixes were made.

## Practical limits

The queue table can scroll horizontally on narrow screens to retain full IDs.
Large graphs may require zooming/panning; all supplied payments remain available
in the graph and via citation details. Trace-only references can legitimately
lack an amount or one endpoint; the card does not fill those gaps. Data-source
changes remain deliberate and do not substitute Demo records for Live failures.

The historical real on-demand result is still session-only. The UI redesign does
not persist it or authorise another run. Use the existing verification report or
labelled Demo simulation if the original session is gone. No live permission,
service availability or pipeline-health claim follows from this offline review.
