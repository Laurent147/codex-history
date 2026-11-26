# Codex History

A single-page, local browser for Codex chat history. Python 3.10+; no third-party packages.

```bash
python3 app.py
```

Open **http://127.0.0.1:8765**. Run from a terminal where `codex` is on PATH to enable deletion.

- Search titles, first messages, IDs, directories, models, and branches.
- Enable **Search full conversations** to search user/assistant message bodies too (slower on large histories).
- Filter by directory, archive status, and inclusive creation/update date range. Subagents are hidden by default.
- View conversations, sort columns, and copy `codex resume <id>` commands.
- See sortable total-token and hypothetical API-cost columns, with totals for the current filters.
- Select individual chats or **all matching results**, then delete after reviewing the exact selection. Changing filters clears selection. Maximum 500 chats per deletion.

The app reads `state_*.sqlite` in read-only mode. Conversation previews support legacy JSONL rollouts and the newer `thread_history_*.sqlite` paginated format. It does **not** delete database rows itself: it invokes `codex delete --force <UUID>` for each selected chat, using the configured Codex home. This is permanent. Project directories are not deleted. Errors are reported per session; a batch is not atomic. No chats are deleted when you start the app or browse history.

## Token usage and hypothetical cost

Usage comes from records in each session's rollout file, including the rollout files retained for paginated histories. When available, individual request records are deduplicated by response ID (or rollout ordinal) and summed. This ledger is used when it covers the sidebar database total and the cumulative totals recorded in the file. Otherwise the app uses a complete, consistent cumulative usage stream. Request and UI event counters are kept separate: their baselines can diverge after compaction, which must not be mistaken for a reset. Repeated snapshots are deduplicated by counting only increases within the same stream. Changes in the recorded model are priced separately. Cached input is a subset of input; reasoning is a subset of output, so neither is counted twice. Hover over a token total for the source, sidebar total, and exact breakdown, or a cost for its model breakdown.

The USD estimate is `(input − cached input) × input rate + cached input × cache rate + output × output rate`, with rates per million tokens in [pricing.json](pricing.json). These are **standard text-token API rates**, checked November 26, 2026 against the linked OpenAI sources. For models with a configured long-context rule, individual requests above the threshold receive its input/cache and output multipliers. For example, GPT-6.1 Sol requests over 272,000 input tokens use 2x input/cache and 1.5x output rates. Only individual request sizes trigger this rule; a large cumulative session total must never trigger it. Snapshot-only sessions use base rates. Long-context rules are configured for GPT-6.1 Sol, GPT-6 Sol, GPT-6 Luna, and GPT-5.6 Luna; surcharges for other models are not modeled. Estimates exclude fast/priority tiers, regional premiums, tool charges, and taxes. This is an illustrative API equivalent, **not your subscription bill** or historical invoice. Edit the local pricing file and restart to update rates or add a model. The app never fetches pricing or uploads usage.

Missing or genuinely inconsistent rollout usage falls back to the SQLite token total, with no cost estimate. Unknown models and cache-write usage also have no estimate. Cost totals explicitly show partial coverage and the fraction of recorded tokens unpriced when some chats cannot be priced. Request records can include usage absent from the sidebar database after compaction; in that case the app shows the fuller request total and explains the difference in its tooltip. Forked chats can include inherited usage, so summed session totals need not equal unique account-wide consumption. Totals follow the table filters (dates select sessions, not individual requests), and deleted chats no longer contribute. Parsed usage is cached in memory and refreshed when rollout files change.

```bash
python3 app.py --port 8888 --codex-home /path/to/.codex
python3 app.py --codex-bin /absolute/path/to/codex
python3 -m unittest discover -s tests -v
```

`CODEX_HOME` is honored, otherwise `~/.codex` is used. The highest numbered state/history database is selected. Metadata schema fields are detected to tolerate older Codex versions. The installed CLI must support `delete --force`.

The server binds to `127.0.0.1` only. No external scripts, fonts, APIs, telemetry, or Python dependencies. Requests are checked for a local Host and same origin; deletion also requires a per-server token. Do not put this personal tool behind a public proxy.

Limitations: compressed legacy `.jsonl.zst` transcript preview/search is not supported; metadata remains browseable. Full conversation search excludes tools and reasoning. Previews show plain text and are limited to the first 1,000 messages or 2 MB. Filesystem changes are picked up on refresh. Session availability and deletion of running sessions are governed by Codex CLI. Dates assume the browser and server share a timezone. Cloud-only conversations are not included.
