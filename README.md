# Conso Multi-Account Automation

Rewrite of the Conso zap automation with multi-account support and proactive
token refresh, built on the same reverse-engineered API surface. Every
production-hardened constraint from the empirical work carries over.

## What it does

- Runs earning sessions for one or many Conso accounts in parallel.
- Refreshes the Supabase JWT proactively (before `exp`) instead of waiting
  for a 401/403, and persists the rotated token so an operator never pastes
  by hand.
- Applies every measured server constraint: locked (model, platform) set,
  per-credit ceiling, shared daily budget, zero-credit soft-flag abort.
- Keeps per-account state in `data/state/<label>.json` and account
  credentials in `data/accounts.json` (chmod 600 on save).

## Layout

```
main.py                entry point
conso/
  __init__.py          package version
  config.py            Settings from .env + directory layout
  accounts.py          AccountStore: multi-account JSON, atomic writes
  state.py             AccountState: per-label daily checkpoint
  auth.py              AuthSession: proactive JWT refresh
  client.py            ConsoClient: async Supabase RPC with retries
  session.py           run_earning_session: locked earning loop
  runner.py            multi-account fan-out with bounded concurrency
  cli.py               argparse subcommands
  prompts.py           prompt corpus (long + short)
  zaps.py              zap formula and quality scorer
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

The extension stores its Supabase session in `chrome.storage.local` under
`sb-jzxlayjrsdbyzykuiqns-auth-token`; the browser network tab at
`conso.xyz` exposes the same tokens. You need `access_token` and
`refresh_token` for every account.

## Adding accounts

```bash
python3 main.py accounts add primary
# prompts for access_token and refresh_token (hidden input)

python3 main.py accounts add alt1 --note "backup account"

python3 main.py accounts list
python3 main.py accounts disable alt1     # skip in runs, keep credentials
python3 main.py accounts enable  alt1
python3 main.py accounts remove  alt1
```

`accounts add` upserts by label — re-running with the same label rotates
credentials in place.


## Interactive menu

`python3 main.py` with no subcommand opens a zero-dependency stdlib menu.
Actions call the same runner primitives the CLI does, so there is one
behaviour to reason about, not two.

```
python3 main.py             # default: menu
python3 main.py interactive # explicit
python3 main.py menu        # alias
python3 main.py tui         # alias
```

Menu keys:

| key | action |
|---|---|
| 1 | list accounts (also shown on every loop) |
| 2 | add / update account (prompts securely for tokens) |
| 3 | enable account |
| 4 | disable account |
| 5 | remove account |
| t | test — auth + profile pre-flight |
| s | status snapshot |
| r | force JWT refresh |
| e | earn (asks platforms, corpus, attachment, dry-run) |
| p | show the locked (model, platform) set |
| q | quit |

Account pickers accept comma-separated indices or labels; blank input
targets every enabled account. `Ctrl-C` inside an action returns to the
menu without exiting the process.

## Live dashboard (curses TUI)

```
python3 main.py dashboard   # or: tui
```

Curses UI, zero deps:
- **Header:** clock, account count, running-task count, banned count, cap,
  ceiling, pacing, parallelism, dry-run.
- **Totals row:** aggregate server zaps and today's server total across
  every visible account.
- **Account table:** label, consoname, state, local today (from
  `data/state/<label>.json`), server daily (from `daily_zaps_earned`),
  server total (`total_zaps`), auth TTL (counts down live).
- **Activity log:** per-account events (earn credits, refresh, poll errors)
  streamed newest-first.
- **Footer:** keybindings.

Keys:

| key | action |
|---|---|
| `e` | start earn wave for every enabled account |
| `E` | start earn wave for the highlighted account only |
| `r` | force JWT refresh for every enabled account |
| `t` | pre-flight (auth + profile) for every enabled account |
| `x` | signal every running wave to stop |
| `↑` / `↓` / `k` / `j` | move selection |
| `space` | toggle enabled state on the highlighted account |
| `p` | poll profile snapshot now |
| `q` / `Esc` | quit |

All actions run on a background asyncio loop; the UI stays responsive.
Profile snapshots also auto-poll every 30 s so `daily_zaps_earned`
converges without operator input, and auth TTL ticks down every second
with a warning colour when it enters the refresh window.
## Daily use

```bash
python3 main.py test                     # pre-flight every enabled account
python3 main.py earn                     # earn on every enabled account
python3 main.py earn --label primary,alt1
python3 main.py earn --dry-run           # plan only, no writes
python3 main.py status                   # profile snapshot per account
python3 main.py refresh                  # force one JWT rotation per account
python3 main.py platforms                # show the locked (model, platform) set
```

Every runner subcommand takes `--label <a,b>`; the default is every enabled
account. `earn` accepts `--platforms`, `--short`, and `--no-attach` in
addition to the label filter.

## Concurrency and pacing

`.env` controls the runner:

| var | meaning | default |
|---|---|---|
| `CONSO_MAX_PARALLEL` | accounts running append_prompt concurrently | 4 |
| `CONSO_ACCOUNT_STAGGER` | seconds between account launches | 15 |
| `CONSO_GAP_MIN` / `CONSO_GAP_MAX` | uniform range between turns on one account | 60 / 180 |
| `CONSO_REFRESH_LEEWAY` | refresh window before `exp` (seconds) | 90 |
| `CONSO_DAILY_ZAP_CAP` | shared daily budget per account | 30 |
| `CONSO_CREDIT_CEILING` | per-credit hard ceiling (server-side) | 3.2 |

The runner shuffles account order every wave so a repeat run never hits the
same account first — a minor anti-fingerprint measure.

## Token refresh

`AuthSession` decodes the JWT `exp` claim once and refreshes when the token
is inside `refresh_leeway` seconds of expiring. If a request still comes
back 401 or 403 `bad_jwt` (which can happen after a client-side clock skew
or a manual sign-in elsewhere), a reactive refresh runs and the request is
retried once. On success the new access + refresh tokens (Supabase rotates
both) and the new `exp` are written back to `data/accounts.json` atomically.

A revoked session cannot be recovered by any refresh: signing in again on
the same account invalidates the previous session, and the old refresh
token then fails with `session_not_found` / `refresh_token_not_found`. In
that case, re-run `accounts add <label>` with the new tokens.

## Multi-account semantics

- **Isolated state.** Each label owns `data/state/<label>.json`. Switching
  the underlying account (same label, different `user.id`) resets the
  checkpoint automatically.
- **Isolated failure.** One account's ban, auth failure, or crash never
  aborts the others — each returns its own `RunOutcome` and the wave keeps
  going.
- **Bounded parallelism.** `CONSO_MAX_PARALLEL` caps the number of accounts
  running `append_prompt` at once; the extras wait behind an `asyncio`
  semaphore. Set it to 1 for strict serial behaviour.

## Anti-abuse notes

- Payloads are scaled to land under the empirical `3.5`-zap-per-credit
  ceiling; `credit_ceiling` defaults to `3.2` for margin.
- Two consecutive zero-credit responses on one account abort that account's
  session — the soft flag that precedes a ban.
- The extension talks to four platforms and each request carries a
  platform-appropriate model id; anything outside the locked set raises
  before hitting the wire.

Do not run this on an account you cannot afford to lose. Same threat model
as the original tool — the backend actively scores scripted cadence.

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | one or more accounts errored |
| 2 | argument or auth-store error |
| 3 | ban detected |
