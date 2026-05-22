# Maxwell REM

Maxwell REM is a Maxwell-bot translation of Dirac's REM dreaming feature. It keeps the same intent: periodically consolidate the short-term visible slice of what users said and what Maxwell visibly sent into durable long-term memory.

REM is not a live chat reply. While the bot is alive, a background loop can run an opt-in dream pass that reads `data/rem_events.json`, consults `long_term_memory.txt`, and uses a tiny private tool surface to add, edit, search, or remove long-term memory lines. It never sends messages to Discord channels.

The visible event log is separate from Maxwell's existing `memory.json` channel memory. It stores only plain visible user and assistant text, plus compact media markers such as `[image]`, `[audio]`, `[video]`, `[file]`, or `[embed]`; model reasoning and `<think>` blocks are stripped before persistence.

State lives in JSON under `DATA_DIR`:

- `rem_events.json`: capped global event ring.
- `rem_state.json`: last run timestamp and running flag.
- `rem_runs.json`: capped audit history.
- `rem_control.json`: runtime enable/disable and restored defaults.

Operators can inspect and control REM with `,rem`, `,rem now`, `,rem on`, `,rem off`, `,rem audit [N]`, and `,rem fix`, or through the dashboard REM card and `/api/rem/*` endpoints.
