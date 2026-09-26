# Design notes

How the bot works and why, for whoever maintains it.
For installing and running it, see the README in the project root.

Everything that is not operator-facing lives under `app/`: the package in `app/bulkdn`,
the suite in `app/tests`, tool settings in `app/dev`, these notes and the pinned
dependency list in `app/docs`, the vendored SDK in `app/vendor`, the virtualenv in
`app/.venv`, and runtime state in `app/state`. The root holds what an operator touches:
`install.bat`, `update.bat`, `run.bat`, `settings.yaml`, `private_key.local`,
`proxy.local`, `logs.txt` (and its rolled copies), the README and the Russian guide
`ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md`.

## What it trades, and with whom

The bot trades a **pool** of accounts: every master key in `private_key.local` (one per
line) and every sub-account under each of them. Sub-accounts are not configured — each
key's master is asked for its `subAccounts` at startup (`accounts.discover_accounts`),
so the pool can never contain an account the key cannot sign for. Sessions are named by
position: `m2s3` is the third sub-account of the second key.

`mode` (config.py) decides which keys are in play:

| mode | pool |
|---|---|
| `multi` (default) | every master and every sub-account |
| `single` | one master's own tree; `single_master` picks the key by its line number |

The narrowing happens once, in `Runtime._keys_in_play`: in `single` the other masters get
no session at all. Everything downstream — drawing, trading, accounting — is one code
path. With one key in the file the two modes are identical. `pool` is accepted as an old
spelling of `multi`.

Every **market** with `enabled: true` trades, in both modes. A market is a leg config
(size, offset, chase settings, leverage), not a pair of accounts: the old block names
`master_account` / `sub_account` are only names.

Each cycle, `Pairing.draw` builds a **group** for one market:

```
maker     one account; rests a limit order (post-only), chased toward the touch
takers    1..max_takers other accounts; each fill is hedged at market across them,
          in shares fixed when the group is drawn (no share under 15% of the cycle,
          and never one below the market's minimum order -- asking for more pieces
          than the size can carry yields fewer pieces)

OPEN      maker rests the entry  --every fill-->  takers hedge their shares
HOLD      hold_minutes, drawn per cycle, kept as a deadline in the state file
EXIT      maker rests the close (reduce-only)  --every fill-->  takers buy/sell back
```

The accounts do **not** swap between OPEN and EXIT in a group. Two accounts could swap;
three cannot — one hedger would become the maker and the other two would hold shorts
nothing closes. So the opener closes what it opened and every hedger closes its share.
The maker's side is chosen per draw (`_least_crowded_side`, a coin flip on a tie), not
fixed long.

Rules `pairing.py` holds: an account is in at most one group at a time (the hedge is
derived from positions, and one position cannot serve two sums); a group never contains
an account twice; with more than one master in play, the maker comes from one master's
tree and every taker from another's. Up to `pool.max_groups` groups run at once.

The group disbands at the end of its cycle and its accounts go back to the pool. The
per-cycle draw of accounts, side, size and offset exists so that a series of trades does
not repeat one shape.

---

## How hedging actually works

The strategy is stated as *"every fill triggers one opposite market order"*. That is what
happens on the happy path, but it is **not** how it is implemented, and the difference
matters.

The hedge size is **derived from position state**:

```
net = position[maker] + sum(position[taker_i])
if |net| >= tolerance:
    market order of -net, split across the takers by their shares
```

Fill events are only a low-latency *trigger* to re-evaluate. This is behaviourally
identical on the happy path — a 0.10 BTC partial fill moves net to 0.10 and immediately
produces a 0.10 BTC market hedge — but it is **idempotent**: running it twice is a no-op,
a missed fill is caught by the next trigger or the reconciler, and crash recovery needs no
fill journal. On restart the bot reads every account, computes net per group, and corrects
it.

Fills do carry a `tradeId` now (API v1.0.17), and it is used — see "API compliance" — but
only to keep a replayed fill from being applied to the optimistic overlay twice. An id
helps only with fills that *arrive*; it says nothing about fills missed, reordered, or
landed while the process was dead, which is why the hedge stays position-derived.

### One path for every hedge

Every hedge in a run — the hedge worker's and the periodic reconciler's — goes through
`Strategy._hedge_leg`:

1. **Pull our own orders out of the way first** (`_clear_hedge_path`). A hedge covers a
   maker that bought with a market SELL, which sweeps the bid — where the unfilled rest of
   that same maker's order is resting, first in the queue, because the chaser puts it
   there. Two accounts under one master are matched by the exchange like strangers, and
   that volume is excluded from the fee tier. So every resting order of ours in that
   market on the swept side is cancelled first, concurrently, skipping orders already
   filled. A cancel that fails does not stop the hedge: unhedged exposure has no bounded
   cost, a self-trade costs a fee.
2. **Mark the side as being swept.** While the hedge runs, `_drive_leg` places nothing on
   that side. A chaser mid-placement has no order id yet for step 1 to cancel, and a
   second group re-placing onto the swept side is what turned one self-trade into a
   ping-pong on a live run.
3. **Send the slices concurrently** (`Hedger.hedge`), reserving each in `InFlight` before
   it goes out, so a fill that arrives before `market()` returns can retire it.

`_least_crowded_side` keeps groups on one market on opposite sides where it can, so the
ordinary case cancels exactly one order — the filling leg's own.

Recovery's one-off reconcile at startup calls the hedger directly; it runs after every
resting order has already been cancelled, so there is nothing in its path.

### Concurrency

The hedge worker drains fill signals and starts **one task per leg**. Groups share no
accounts, so nothing orders their hedges; the worker used to hedge them in turn, and a
hedge that waits over a second gave back 2.6bps against 0.8 under half a second. The same
leg is never hedged twice at once: a signal for a leg whose hedge is running is folded into
one more pass after it finishes. A `Hedger` lock per leg key backs this up.

### A hedge with no answer

A slice that gets an explicit refusal (`OrderRejected`, which includes
`TransactionRejected`) never traded: its reservation is released and the next trigger
retries exactly that part.

A slice with **no answer** — a timeout, a dropped socket — may have executed. Releasing it
re-hedges exposure that may already be covered (a live run sent three duplicate hedges
that way), and letting it lapse after the usual `overlay_ttl_ms` does the same a little
later. So it is held as a *doubtful* reservation (`InFlight.hold_in_doubt`, up to 30s) and
`HedgeInDoubt` is raised. `_hedge_leg` then marks the book **suspect**: the worker and the
reconciler both refuse to hedge until a fresh position read succeeds (`_refreshed`). That
read retires the doubtful reservation (`settle_doubtful`) — only a read that *began after*
the slice was sent, because only that one reflects whatever the slice did.

### Position reads

The SDK's HTTP client is synchronous, so position reads run in a thread. Their answers are
**applied on the event loop** (`reconcile.sync_positions` → `PositionBook.apply_read`),
because the book is the loop's: fills and position updates land in it there and the hedger
reads it there.

`apply_read` keeps anything newer than the read. The answer is roughly a round trip old
when it lands (~320ms from a PC far from Tokyo), and a fill that reached us over the stream
in that window is newer than it; writing the read over it made the fill vanish and the
reconciler hedged it again. So a key the stream has touched since the request was sent is
skipped.

---

## Setup

`install.bat` is the installer, and the only supported one. In order it:

1. checks `python` and `git` are on PATH, and that Python is **64-bit 3.12 or newer**
   (the pins need it: numpy 2.5 is the floor, and numba/llvmlite publish no 32-bit
   Windows wheels). 3.12–3.14 is what has been run; newer gets a warning.
2. creates `app/.venv` (`python -m venv app\.venv`), and uses `proxy.local` for pip when it
   holds an address.
3. installs the SDK from the **vendored wheel** `app/vendor/bulk_client-0.1.2-py3-none-any.whl`
   with `--no-deps --force-reinstall`, falling back to the pinned GitHub commit
   `3a6506e` only when the wheel is missing (a copy that predates it).
4. installs everything else from **`app/docs/requirements.txt`** — the one pinned list.
5. creates `settings.yaml`, `private_key.local` and `proxy.local` if absent, and runs the
   signing check.

Why each of those:

> **The PyPI build of `bulk-client` 0.1.2 cannot sign.** Its signer omits the trailing
> signature-domain byte (`mainnet=1`) that the API requires in the preimage, and it has no
> `SignatureDomain` type at all, so every signature is rejected as `bad signature`. The
> vendored wheel is the good build, from GitHub at `3a6506e` — **with the same filename as
> the bad PyPI one**, so the filename proves nothing; the signing check does.
>
> Pin the commit. Older commits of the same `0.1.2` version parse the account stream's
> margin with the wrong field names (`totalBalance` where the API sends `totalMargin`),
> silently reporting a zero balance, and cap WebSocket frames at 16 MiB where the spec
> allows 64 MiB. `--force-reinstall` is there because the version string does not change
> between commits, so pip would otherwise keep whatever an earlier run left.
>
> `--no-deps` because the package declares `bulk-keychain` and `solders`, imports
> neither, and `bulk-keychain` has no prebuilt wheel for current Pythons — pip would try to
> build it from Rust and fail. The SDK's real dependencies are listed by hand in
> `requirements.txt` instead, which is why a plain `pip install -r` of that file is not a
> complete install on its own. pip prints a red `ERROR` about the two missing packages;
> install.bat says beforehand that it is expected.

`app/vendor/README.md` says what has to move together when the SDK does (the filename in
install.bat and the fallback commit).

The bot itself is not installed: `run.bat` puts `app/` on `PYTHONPATH`, so
`python -m bulkdn` finds the package where it lies. Test tooling (pytest,
pytest-asyncio, ruff) is deliberately not installed; `app/tests/run-tests.bat` prints the
command to add it.

Verify the signer is the domain-aware one before trading:

```bash
app/.venv/Scripts/python -c "from bulk_api.common import SignatureDomain; print(list(SignatureDomain))"
```

If that import fails, you are on the PyPI build and **must not** trade with it.

### Starting it

On Windows, use the launcher — it picks the project's interpreter for you:

```
run.bat              opens the menu
run.bat status       any CLI subcommand works
run.bat run --live
```

Plain `python -m bulkdn` is the usual first mistake: it takes whichever Python is first on
PATH, and a global install carrying an older `bulk_api` fails with a missing
`SignatureDomain`. `app/bulkdn/__main__.py` detects that and prints the interpreter to use, but the
launcher avoids the choice entirely.

### Keys

Signing keys come from **`private_key.local`**, one master key per line, read by
`keystore.load_all`. The file holds either plaintext lines (comments and blanks ignored,
duplicates dropped, order kept — the first key is the one single-account commands use by
default) or one encrypted envelope covering all of them; see "The private key" below.
`BULK_PRIVATE_KEY` in the environment overrides the file, with keys separated by newlines
or commas. The settings file never holds a key.

### Sub-accounts

The bot finds sub-accounts itself; nothing goes in `settings.yaml`. (`sub1_pubkey` is no
longer read; the config loader warns that it can be deleted.) Every key needs at least one
sub-account, or startup stops with `master ... has no sub-account`.

It can also create them: `bulkdn create-subaccount --name <name>` or the menu's
**Accounts Management → Create New Subaccount**, which asks which master (with several
keys), validates the name, and asks for a typed `yes`, because a sub-account cannot be
deleted or moved. The SDK's signer has no `createSubAccount` case, so `subaccounts.py`
serializes it by hand, verified byte-for-byte against `bulk-keychain`. `transfer` is done
the same way.

Verify the wiring before trading:

```bash
run.bat check
```

This confirms the market specs load and, when the pool's first two accounts are in one
tree, that the second really is a child of the first. In `multi` those two can be separate
masters, and `check` says so rather than failing.

---

## API compliance

Checked against the OpenAPI spec (v3.0.10):

**1. Network-bound signatures.** The domain byte (mainnet = 1) is appended to
the signing preimage after `nonce || account`. It is trusted client configuration — never a
JSON field or header — and it is **pinned** in `app/bulkdn/config.py`
(`SIGNATURE_DOMAIN_NAME = "MAINNET"`) next to the mainnet URLs. There is no network
selector: a domain byte that disagrees with the host is rejected as `bad signature`, so
the two are fixed together. `bulk_client` 0.1.2 at the pinned commit always appends it,
and the bot no longer probes for a signature dialect.

**2. Bounded pagination on `POST /account`.** Trading uses current-state queries
(`fullAccount`, `openOrders`). Two things read history, and both page:

* `fees.realised_for_trees` walks `{"type": "fills"}` by cursor, 1000 fills a page, at
  most 20 pages per account, for the execution target and the menu's Progress and
  History screens. With `since_ms` it stops at the first page older than the run's start.
* `liquidation.recent_liquidations` queries `{"type": "riskEvents"}` to ask the exchange
  whether a shrunken position was really liquidated.

**3. `tradeId` on fills.** Used to deduplicate replayed fills (`positions.SeenTrades`).
The SDK's WebSocket `Fill` class **does not carry the field**, and its `Fill.from_api`
reads only the long field names while the account stream also sends the short ones
(`sym`, `oid`, `px`, `sz`, `b`, `ts`, `mk`). `ws_compat.apply_ws_compat` replaces
`Fill.from_api` with a parser that accepts both spellings and attaches `tradeId` (or
`tid`) as `fill.trade_id`; `ws_compat.fill_trade_id` reads it back.

Deduplication is scoped **per account**, because the changelog states maker, taker, and
isolated-account views of one execution share a trade id. A global set would discard the second
account's legitimate view of a trade that crossed between two of our accounts. A fill with
no id is always treated as new.

## Testing it

**This bot is mainnet-only.** `mainnet-api1.bulk.trade` / `mainnet-ws1.bulk.trade` are pinned
in `app/bulkdn/config.py` together with the signature domain byte, and there is no network selector.
Every `run --live` trades real money, and `--live` is the only interlock.

**There is no rehearsal environment.** Dry-run (`run` without `--live`) is the only way to
exercise the bot without spending, and it cannot advance past OPEN because nothing fills.
It writes its own state file, emptied at every start, so nothing it records can be resumed
by a live run.

**The WebSocket verifies against certifi, not the system trust store.** This was diagnosed
backwards at first, and the wrong diagnosis is worth keeping because of what followed from it: the
note here used to read "the mainnet WebSocket serves an expired certificate", and the response was
to turn verification off by default.

The certificates are valid. Measured against all three hosts:

| host | system store | certifi |
|---|---|---|
| `mainnet-ws1.bulk.trade` | rejected — certificate has expired | OK, valid to 22 Nov 2026 |
| `mainnet-api1.bulk.trade` | rejected — certificate has expired | OK |
| `indexer.bulk.trade` | rejected — certificate has expired | OK |

What expired is a root in the Windows store. `requests` never noticed because it ships certifi and
uses it — which is why every HTTP call worked while the socket, going through
`ssl.create_default_context()`, failed on the same machine in the same second.

`ws_compat.verified_context()` points the socket at certifi's bundle, so both halves of the bot
trust the same anchors and verification stays on. `certifi` is pinned in `requirements.txt` by
name for that reason.

`ws_ssl_auto_bypass` now **defaults to false**, in `config.py` and in the shipped settings. It
survives for an operator who cannot connect at all, and when it does fire it says plainly that
fills and positions — the input to every hedge — are then coming from an endpoint nothing has
authenticated.

Test in this order:

**1. Unit tests** — no network, no keys.

```bash
app\tests\run-tests.bat
```

**2. Read-only checks against the live API** — no signing, no orders.

```bash
run.bat check    # specs + sub-account wiring
run.bat status   # every account's positions and open orders, every configured market
```

**3. Dry run** — connects every account stream and logs every transaction it *would* send,
submitting nothing. It cannot advance past OPEN, because nothing fills.

```bash
run.bat run
```

**4. Full cycle, live, minimum size** — there is no free equivalent. Fund the masters
on-chain, move margin to the subs (`bulkdn transfer`, or **Balance All Subaccounts**), set
`hold_minutes: 2`, `cycles: 1`, and the smallest sizes that clear each market's minimum
notional, then run a complete OPEN -> HOLD -> EXIT cycle against the real matching engine.

```bash
run.bat run --live
```

Watch net exposure against the **unhedgeable floor**. A hedge smaller than a market's minimum
notional cannot be submitted, so that floor (not zero) is the best neutrality achievable: ~$50
on SOL and ETH, ~$1 on BTC. It does not shrink by trading larger.

This is also the only thing that exercises per-action signature correctness, rejections
(`rejectedCrossing`, `cancelledReduceOnly`, `rejectedRiskLimit`), the 25 ms taker speed bump,
the `minNotional` floor, and whether pre-computed order IDs match what the exchange assigns.
Keep `run.bat flatten --live` ready in a second window.

## Running

Running it with no subcommand opens the menu, which is the intended way in:

```bash
run.bat
```

`app/bulkdn/__main__.py` is a launcher over `bulkdn.cli`; `python -m bulkdn` and
`python -m bulkdn.cli` reach the same entry point. Both need `app/` on `PYTHONPATH`,
which is what `run.bat` sets.

```text
+--------------------------------------------+
|             DELTA-NEUTRAL BOT              |
+--------------------------------------------+
| 1. Start                                   |
| 2. Active Strategy                         |
| 3. History                                 |
| 4. Accounts Management                     |
| 5. Configuration                           |
| 6. Close All Positions                     |
| 7. Logs                                    |
| 0. Exit                                    |
+--------------------------------------------+
```

The items are numbered from `menu.MAIN_ITEMS`, and the stop banner a live run prints looks
up "Close All Positions" by its number there rather than hard-coding it.

- **Accounts Management**: Create New Subaccount, Balance All Subaccounts (evens
  transferable margin *within* each master's tree — a transfer is signed by the key that owns
  both ends, so nothing moves between masters), Collect Funds to Main, Encrypt Private Key,
  Erase Local Data.
- **Configuration**: Number of Cycles, Total Amount to Burn, Total Trading Volume, Markets &
  Accounts (Accounts: `mode` and `single_master`; Markets: add, remove, switch on/off),
  Progress.
- **Logs**: the tail of `logs.txt` and its full path.

The menu is a front end over the subcommands below, not a second implementation. Rules it
keeps:

- Every item that spends money asks for a typed `yes` first — `y` and a bare Enter both
  abort — because the bot is mainnet-only and there is no harmless mistake. Erasing the key
  needs `DELETE KEY`.
- **Ctrl+C at any prompt cancels** (`menu.Cancelled`): whatever the prompt was about to write
  or send is never reached. It used to turn into the answer `0`, which wrote a burn target of 0
  and once submitted a real `createSubAccount` named `"0"`.
- **Each run gets its own copy of the config** (`menu._fresh`). A run rewrites leg sizes to
  what the thinnest account can carry; with one shared config a dry run's sizes became the next
  live run's. The menu's own edits still land on the original.
- **Signed account actions report what actually happened.** Transfers and sub-account creation
  are retried by re-POSTing the same signed bytes, so a failure after a retry proves nothing: the
  first attempt may have applied and only its answer been lost. The transport marks such a result
  `uncertain`, and the menu reports it as **UNKNOWN** — check balances before retrying — rather
  than "rejected, balances unchanged". A batch of transfers stops at the first UNKNOWN or
  unreachable one and lists what was not sent.

`Total Amount to Burn` and `Total Trading Volume` are enforced: see "Execution targets".

The subcommands stay available for scripting.

**Orders are not submitted unless you pass `--live`.** Without it the bot connects,
subscribes, runs the full phase machine, and logs every transaction it *would* send.

```bash
# dry run on mainnet — connects and logs, submits nothing
run.bat run

# live — REAL FUNDS, no confirmation prompt
run.bat run --live
```

Other commands (`--mode single|multi` overrides the settings file for any of them):

```bash
run.bat               # interactive menu (same as `run.bat menu`)
run.bat encrypt-key   # encrypt the key file, or change its password
run.bat status        # positions, open orders, persisted phase
run.bat check         # validate config and account wiring
run.bat transfer --to <pubkey> --amount <n> [--from <pubkey>]   # move margin within a tree
run.bat create-subaccount --name <name>                          # under the first key
run.bat flatten --live   # cancel everything and close all positions at market
run.bat flatten --live --limit   # the same, but with resting orders (no taker fee)
run.bat flatten --live --limit --limit-timeout 600   # ...and wait longer than the 5min default
```

`flatten` is the manual panic button. It is reduce-only throughout, so it can never open a
new position in the opposite direction. It uses a **slim start** (`Runtime.start(trading=False)`):
connect, load specs, subscribe — no referral check, no sizing plan, no leverage change. Each
of those could fail before a single cancel went out — on an indexer outage, on an account
with no free margin (which is exactly when you want out), on a leverage change the exchange
refused — and none of them has anything to say about closing.

It covers **every market in the settings, enabled or not**, on every account in the pool: a
market switched off with a position still open used to be left on the exchange and
forgotten. A position in a market that is **not in `settings.yaml` at all** has no spec to
size a close with; `flatten` names it, exits 1, and **keeps the state** rather than reporting
flat. Otherwise a live flatten resets the state (legs, halt, target baseline); a dry run
leaves it alone.

`--limit` is the patient variant. It rests one tick inside the touch, re-prices as the touch
moves, and never crosses, so every fill is on the maker side -- no spread paid, no taker fee.
The trade is certainty: it fills only when someone trades with it. On timeout it cancels its
orders, logs what is still open, and exits non-zero, because "I could not finish" and "you are
flat" must not look the same to whatever called it. An interrupt cancels the orders too --
leaving reduce-only orders resting with nothing watching them is the one outcome worse than
either close.

### Stopping

Pressing `S` in the run's window calls `Strategy.request_stop`: a flag, not a cancel, so an
order already being submitted completes and is accounted for. Groups mid-cycle stay
registered; once the legs have left their loops the run cancels every resting order, then
makes **one last hedge pass** (`_last_hedge_pass`: a fresh position read and the reconciler
over every registered leg). The cancel sweep goes account by account and can take tens of
seconds over a large pool; a maker fill in that window used to be booked and never hedged.
Positions are then logged and left open, hedged — closing them is **Close All Positions**.

---

## Safety

The strategy as originally specified has no halt condition. This implementation adds one,
because the bot fires market orders unattended: if a hedge leg starts failing (insufficient
margin on a hedger, a risk-limit rejection, a dead socket), the position silently stops being
neutral and nothing on the happy path notices.

Any of these cancels all orders, flattens every account, records the halt, and stops:

| Limit | Meaning |
|---|---|
| `max_net_exposure_usd` | Uncorrected directional exposure — per market over all accounts, **and per group** over that group's accounts. Summed over everything, two groups on one market at +$300 and −$300 read $0 while each sat directional |
| `max_position_usd` | Per-account, per-symbol notional ceiling |
| `max_reject_streak` | Consecutive rejected transactions on one account |
| `ws_stale_timeout_s` | No WebSocket traffic for this long, or a socket down (after up to 3 heal passes) |
| hedge ceiling | A single required hedge above 2× the leg size — means the book and reality have diverged |
| `max_phase_minutes` | OPEN or EXIT running this long; HOLD is exempt |
| liquidation guard | A position closed from outside — see below |

Risk checks deliberately use **confirmed** positions only. A limit should not be satisfied
by a fill the exchange has not acknowledged. A market with no price uses its **last known
price**, with a warning: a missing ticker used to read as $0, which made every exposure $0 and
let the limits pass for exactly as long as the feed was down.

### Liquidation guard

`LiquidationGuard` watches every account's position for a shrink the bot did not cause. Before
calling it external it (1) defers, a bounded number of times, when an order of ours in that
symbol went unanswered *and* a forced position read succeeds, and (2) re-reads positions once
more to rule out a stale HTTP answer racing our own fill. Then it asks the exchange
(`riskEvents`); failing to ask counts as yes.

When it acts, **hedging is suspended before the first close goes out** (`_closing_out`, also
true for the whole of a halt): on a live run the worker "hedged" the guard's closes as they
filled and left the closed accounts holding fresh opposite positions. It cancels resting orders
in the affected markets, closes every account there reduce-only, and **always raises the
halt** — in a `finally`, so anything escaping the closes still ends in a recorded halt rather
than a generic failure that flattens nothing.

### When the run fails for any other reason

Any exception out of the run — not only a halt — **cancels every resting order** before it
propagates, and logs what is open. It used to leave through `finally`, which stopped the hedge
worker and pulled nothing: every other group's order stayed on the book with no hedge coming.

The risk supervisor and the hedge worker are **watched** (`_until_legs_finish`). If either dies,
or returns without a stop having been asked for, the run fails with that as the reason, rather
than trading on with no risk limits or no hedging.

### Crash recovery

State is persisted atomically (temp file + `os.replace`) after every transition. A write that
fails (OneDrive, antivirus) is logged and trading continues — the file is a hint about phase,
not the ledger. On startup the bot:

1. Reads every account's positions over **HTTP** — the WS snapshot has not arrived yet,
   and an empty book is indistinguishable from being flat.
2. Refuses to start on a recorded **halt**, until `flatten --live` clears it. A halt means
   something went wrong; restarting past it unread is how the fault repeats. Resting orders
   are cancelled first — refusing to start is no reason to leave them working.
3. **Restores the groups** that were mid-cycle from the state file (`restore_groups`: each leg
   carries its group id, maker, takers, shares and side), and reserves their accounts so no new
   group can draw them. A group whose key is no longer in the key file is reported loudly and
   left alone — its position is real and needs that key.
4. **Refuses positions nobody owns** (`_refuse_unowned_positions`): a position above dust on an
   account no restored group owns would never be closed, and a new group drawing that account
   would read it as its own imbalance. Resting orders are cancelled before refusing.
5. Cancels all orders. A resting order that cannot be matched to the recovered plan is an
   unhedged fill waiting to happen.
6. Re-runs the hedge rule on the restored groups to correct inherited exposure.

A restored group **finishes its cycle** from where it stopped (`_run_leg` enters only the
phases the leg has not passed): a group caught in OPEN moves on to its hold with what it
already holds, HOLD finishes the hold it was serving (the deadline is persisted), and EXIT
resumes the close — `_leg_exit` re-derives its target from what the maker actually holds. A
group resumed mid-EXIT used to fall through to COMPLETE and release its accounts with the
position still open; and the cycle-count and target checks now run only at a cycle boundary,
so a resumed group is never released early because a target was met meanwhile.

Group ids are **never reused across runs** (`Pairing.skip_ids_through`): a new `g1` used to
inherit the old `g1`'s leg — offset, order cap and cycle count. Finished legs (COMPLETE or
never left IDLE) from earlier runs are dropped from the state file; they hold nothing.

A corrupt state file is fatal rather than ignored — it may be the only record that
positions are open.

---

## Configuration

See `settings.yaml` (shipped as `app/settings.default.yaml`, which the bot never reads). The
parameters that shape execution:

| Key | Effect |
|---|---|
| `mode` | `multi` (every key) or `single` (one key's tree) |
| `single_master` | Which key, by line number from 1, in `single` |
| `pool.max_groups` | Groups trading at once |
| `pool.max_takers` | Accounts one hedge may be split across; 1 = no split |
| `execution_target.cycles` | Groups the run starts (resumed ones included); 0 is unlimited |
| `execution_target.burn_usd` | Stop once this much has been paid in fees this run; 0 disables |
| `execution_target.volume_usd` | Stop once this much **qualifying** volume is done this run; 0 disables |
| `legs.*.enabled` | Whether the market trades; its numbers stay in the file either way |
| `legs.*.leverage` | Max leverage for that market, set on every account in the pool; omit to keep each account's own |
| `risk.max_hedge_impact_bps` | Refuse a hedge whose predicted slippage exceeds this; 0 disables |
| `legs.*.notional_usd` | Dollars the cycle accumulates; a number or a `low-high` range drawn per cycle |
| `legs.*.size` | The same, as a base quantity. One or the other per leg, never both |
| `legs.*.offset_bps` | How far inside the touch the resting order sits; may be a range |
| `legs.*.max_distance_bps` | Drift that triggers a cancel+replace |
| `legs.*.chase_patience_s` | Unfilled for this long: give up the offset and rest on the touch. Still passive |
| `legs.*.improve_ticks` | Ticks to post PAST the touch once tightened, clamped inside the spread. 1 = best bid/ask outright; 0 = join the queue |
| `legs.*.join_depth_usd` | Once tightened, rest at the best level (within 1bps of the touch) where others hold at least this many dollars, our own orders not counted; keep a level while it holds half that; move when it thins. Overrides `improve_ticks`. 0 = off |
| `legs.*.max_order_notional_usd` | Cap on any single resting order, in dollars |
| `legs.*.max_order_size` | The same cap, as a base quantity |
| `hold_minutes` | Time fully open before exiting; a number or a `low-high` range drawn per cycle |
| `max_phase_minutes` | Cap on OPEN or EXIT. A pool group that hits it is cut short (an open keeps what it filled, an exit closes the rest at market) and the run goes on; a configured leg, or a cut that does not finish within 5 minutes, halts, which cancels and flattens. HOLD is exempt |
| `chase_interval_s` | How often resting orders are re-evaluated |
| `reconcile_interval_s` | How often the reconciler re-runs the hedge rule |
| `position_sync_interval_s` | Backstop HTTP position read; a quiet socket triggers one sooner |
| `hedge_tolerance_lots` | Net exposure tolerated before hedging (must be ≥ 1 lot) |
| `overlay_ttl_ms` | How long an unconfirmed fill is trusted before falling back to exchange truth |
| `max_margin_fraction` | Fallback sizing budget; see "Sizing" |

**Validation** (`config.py`). Every value is parsed by a helper that names the setting in its
error: a non-number where a number belongs (or `.inf`/`.nan`, or `true`) is a `ConfigError`
naming the key; a whole-number setting refuses `2.5` rather than truncating it; quoted
booleans (`"false"`, `"no"`, `"off"`) are read as what they say instead of as a truthy string.
**Unknown keys are warned about, not refused** — with the nearest real key (`difflib`), or, for
a retired key like `sub1_pubkey`, a note that it can be deleted — because a newer file opened by
an older build must still start. A settings file that is not UTF-8 (Notepad on a Russian
Windows can save cp1251) gets a sentence saying how to re-save it, not a byte offset.

The mainnet URLs come from the OpenAPI spec's Base URLs and are pinned in `app/bulkdn/config.py`
alongside the domain byte, so the two cannot drift apart. Override them with `http_url` /
`ws_url` in the config only if the published hosts change.

### Sizing

`Runtime.apply_sizing` converts dollar legs to quantities at the HTTP mark price, then weighs
the cycle against **every account in the pool** — any of them can be drawn as a maker, so the
thinnest one binds. `sizing.plan_sizes`:

- If the configured cycle needs at most **80% of the smaller account's available margin**
  (`MARGIN_HEADROOM = 0.8`), it is used exactly as written. The rest is what stands between
  an individually directional account and liquidation while its hedge is in flight.
- Otherwise every leg is scaled down together until the cycle fits inside
  `max_margin_fraction` of that margin, with a warning naming the numbers.
- A scaled leg under one lot or the market's minimum notional is an error, not a rejection at
  the first order.

A range is planned from its high end, so every later draw fits a budget already checked.

**Minimum order size** is `marketdata.min_order_size`: the larger of one lot and the minimum
notional in base units, **rounded up** to a lot. Rounding down landed just under the floor
(SOL at $143.37 with a 0.01 lot gave 0.34 = $48.75 against $50), and every order sized at that
"floor" was refused until the reject streak ended the run.

---

## Implementation notes

Things about the SDK and the exchange that shaped this code:

**Sub-account routing.** `BulkWebSocketClient.place_orders` hardcodes
`account = signer.public_key`, so it can only trade the signing account. The wire protocol
is more capable — `account` is who is acted on, `signer` is who authorises, and a master may
sign for its sub-accounts. `RoutedWsClient.submit` in `app/bulkdn/accounts.py` rebuilds the
transaction with both fields set correctly; the signing path itself is untouched. One socket
per key serves all of that key's accounts.

**Handlers run inside the receive loop.** The SDK awaits event handlers inline in its
WebSocket read loop, and order responses are resolved by that same loop. A handler that
awaited an order submission would block the loop that has to deliver its response, and
deadlock until timeout. So handlers here are synchronous — they update the position book and
signal a worker task, and all order submission happens outside the loop. Blocking HTTP (position
reads, fill history) goes to a thread for the same reason.

Order IDs are computed client-side (a hash over the signed fields), so the bot knows an
order's ID before its response arrives. That is what makes an order placed just before a
crash still recognisable on restart.

**Retries.** `retry.py` retries only transient faults — connection errors, timeouts, 5xx and
429. A rejected order, a bad signature, or an `InvalidURL` fails identically every time and is
not retried (`OSError` is deliberately not a catch-all: `requests`' own exceptions subclass it).
A signed transaction is retried as the **same bytes**, never re-signed, so a lost response
cannot become a second order; `post_signed` marks the result `uncertain` when an attempt may
have reached the exchange unanswered.

**The market feed.** `MarketFeed.quote` treats a book as **frozen** when the mark price has
left its touch by more than 10bps and the book is more than 5s old: its delta subscription can
fail quietly, and a stale touch priced post-only orders that were rejected as crossing or rested
where the market had left. The touch is dropped and pricing falls back to the mark, with one
warning until the book moves again.

**The chaser** (`chaser.py`):

- It does not step past **its own order at the touch**. `chase_price` steps `improve_ticks`
  past the best price on our side, and when that best price *is* our order, following it
  walked the order across the spread one tick per replace.
- **Patience** (`chase_patience_s`) is measured from the first placement of the leg **in that
  direction** — entry and exit are different sides of the book, and patience spent opening says
  nothing about the close. Replaces do not reset it.
- A cancel+replace refused as a whole because its **cancel half** failed (typically the old
  order had just filled) can still have placed the new order. That order is **adopted** rather
  than forgotten — forgetting it left an untracked order, the next pass placed another, and the
  leg filled past its size — and it is resized if it is now bigger than the leg needs.
- It places nothing on a side a hedge is currently sweeping (see "One path for every hedge").

**Telegram** (`notify.py`). Cycle reports and halt alerts are sent in the background
(`send_soon`) so a cycle never waits on Telegram or on the fill-history walk its progress line
needs; `cmd_run` drains pending messages for a few seconds before exiting. An http(s) proxy
from `proxy.local` is used for them; SOCKS needs `aiohttp_socks`, which is not installed, so a
SOCKS proxy is skipped for Telegram with a one-time warning (trading traffic still uses it).

---

## The private key

`private_key.local` is git-ignored and holds either bare base58 seeds, one per line, or an
encrypted envelope. Encrypt it once:

```bash
run.bat encrypt-key
```

```text
Enter password to encrypt privatekeys (empty for default):
Repeat:
```

Every key goes into **one** envelope (`keystore.save_all`). Writing back only the first would
report success and silently discard the others. One envelope rather than one per line, because
a file where some keys are encrypted and some are not is protected by its weakest line. A
consequence: an encrypted file cannot be edited by hand, so keys are added before encrypting.

Argon2id derives a key from the password and XSalsa20-Poly1305 encrypts the
seeds under it, both from PyNaCl -- the same library the SDK signs with, so this
adds no dependency. The KDF parameters are stored in the file, so one written
today still opens after they are raised. Poly1305 authenticates the ciphertext,
which is why a wrong password is reported as wrong instead of returning a
plausible-looking key.

The file is written to a uniquely named temporary file and moved into place, and the envelope
is opened again before the old file is replaced, so an interrupted write cannot
leave a key that is neither the old one nor the new one.

Afterwards every command that needs to sign asks for the password once at
startup. Set `BULK_KEY_PASSWORD` to run unattended; `BULK_PRIVATE_KEY` still
bypasses the file entirely.

> **An empty password uses a constant published in `keystore.py`.** It keeps the
> key off the screen and out of a casual backup. It stops nothing else: anyone
> with the file and this repository can open it. The prompt says so, and so
> does the menu.

Encrypting does not erase the plaintext that was there — it may survive in
backups, editor swap files, or shell history. Rotate the key if that matters.

## Execution targets

Whichever of the three limits is reached first ends the run. They are checked
**between cycles**, never inside one: a target met halfway through an open
position is not a reason to abandon it, or the group is left directional. The
dispatcher stops drawing new groups; the ones already open finish their cycle.

`cycles` counts **groups the run has started**, resumed ones included. A drawn group lives
for exactly one cycle under a key of its own, so the per-leg count never passed 1 and
`cycles: 1` traded until someone pressed stop.

`burn_usd` and `volume_usd` are enforced (`Strategy._target_reached`, asked by the
dispatcher and at every leg's cycle boundary). They are measured by walking the fill history
of every account in the run (`POST /account {"type": "fills"}`) and summing what the exchange
recorded, so they cannot drift from what was actually charged. The walk runs in a thread and
its answer is reused for 30s, because the dispatcher asks every `chase_interval_s` and a pool
of a hundred accounts cannot be paged through twice a second. If the read fails the run
continues -- refusing to trade because a read-only endpoint is down would be worse than
overshooting a soft goal by one cycle.

**Both are measured from the start of the run, not from the life of the account.**
`capture_target_baseline` records a moment, `baseline_at`, in the state file, and the walk
counts only fills since then (`since_ms`). An earlier version recorded lifetime totals and
subtracted them; past the walk's page cap both totals stopped growing and the difference
stopped meaning anything. Before that there was no baseline at all, and the lifetime figure
passing `burn_usd` once ended every later run before it placed an order -- observed live as
`execution target reached: burned $-3.0795 of $3.00`.

The baseline is persisted rather than held in memory so that an interrupted run
resumes its own count: a crash should not hand back progress already paid for.
It is cleared when a run ends on its own terms, and by a live `flatten`, which is the
deliberate way to start a goal over.

**A fee is reported as a negative number.** A taker fill comes back as
`"takerFee": -0.035017`, and a maker fill as `"makerFee": 0.0` -- passive
execution here is free, not rebated. So the summed total runs negative, and
`fees.burned_usd` flips it for anything an operator reads. Printing the raw
figure put a minus in front of every spend line and read as though the account
had earned the money it had spent.

**`volume_usd` counts each trade once, and two figures are reported.** A trade
between two of our accounts appears in BOTH accounts' fill histories. Summing the
two views counted it twice -- a plain bug, fixed by deduplicating on
`(slot, sequence)`, which identifies the trade itself.

Whether such a trade is a **self-trade** depends on the tree: the history is walked per
master (`realised_for_trees`), and only a trade with both sides inside one master's tree is
counted as self-traded. Across two keys the exchange cannot see one owner on both sides.

What to do with self-trades beyond that is not settled, so both answers are shown:

* `qualifying_volume_usd` counts it, like any other trade. On a live account
  this gave $958,778.70 against $958.8K on the referral screen, matching to the
  rounding, so this is the figure the referral programme uses.
* `tier_volume_usd` subtracts it, because the fee documentation states that
  "Self-trades between accounts under the same main account do not create
  qualifying volume".

They are not reconciled because they cannot both be checked yet: the referral window
plainly counts such a trade, while the tier's own `rollingVolume` reads 0 until the
exchange reassesses. `execution_target.volume_usd` is measured against the referral
figure, which is the one an operator can see and compare. Menu item 5 → Progress shows
all of it, all-time and this run, including the self-traded amount on its own line.

## Leverage

`legs.*.leverage` is applied at startup with `updateUserSettings`, to **every account in
the pool**, each signed by the key that owns it -- any of them can be drawn as a maker or
hedger, and sub-accounts copy the master's settings when created and are independent
afterwards. Only what differs is sent, and the market's own ceiling from `/exchangeInfo`
is checked first. `flatten` skips this entirely.

One symbol per transaction, deliberately: the signed bytes are the map's
entries in iteration order, and a multi-entry map has no order the sender and
receiver are guaranteed to agree on. Note the SDK's own `update_leverage`
sends `m` as a list of pairs where the reference client declares a map, so it
is not used.

## Hedge slippage

`risk.max_hedge_impact_bps` prices a hedge against the market's published
impact curve (`GET /impact`) before sending it, and halts rather than trading
through a market that has become too thin. The hedge size is dictated by the
fill it answers -- shrinking it would leave the group directional -- so the only
choices are to pay the slippage or to stop.

**The endpoint answers 404 until a curve is published, and no mainnet market
had one at the time of writing.** With no curve there is nothing to check and
the guard does not fire; startup logs which markets are unguarded.

## Known gaps

- **Dry-run cannot advance past OPEN.** Nothing fills, so positions never move and the entry
  legs never complete. Dry-run verifies connection, subaccount routing, order-ID computation,
  chasing, and the risk checks — it cannot demonstrate a full cycle.
- **No rehearsal environment at all.** The in-process simulator was removed because it modelled
  no signatures, no rejections, no latency and no fees, so it agreed with the bot's own
  assumptions rather than testing them. The bot is now mainnet-only as well, so the first full
  cycle it ever completes will be with real funds. Unit tests cover the phase machine; use the
  smallest sizes that clear minimum notional for the first live run.
- Sub-lot dust is left behind at the end of a cycle. Positions smaller than one lot cannot be
  traded, so a residual below `lotSize` on any account is expected and treated as flat.
- The bot assumes it is the only thing trading the configured markets on the pool's accounts.
  Entry progress is measured from account positions, so an unrelated position would be read as
  strategy fill progress — and at startup, one above dust on an account no group owns stops
  the run.
- Whether a failing `cx` inside a batch aborts the whole transaction or lets the `l` through
  is not documented. The chaser assumes the worst and sweeps superseded order IDs that are
  still resting.
- The OpenAPI spec documents no rate limits. The exchange has answered 429 to two accounts
  polling every five seconds, which is why `max_groups` is a setting and the chase loop is
  throttled conservatively.
- Funding costs are not modelled. A delta-neutral group still pays or receives funding on
  every leg, which is P&L this bot does not account for.

---

## The referral gate, and what it is worth

The bot refuses to start a trading run unless the first key's master account signed up
through the owner — by referral code or by redeemed invite, both of which are checked,
because on the wallet this was built for roughly two thirds arrived by invite. `flatten`
does not ask: closing opens nothing.

### What the public index actually carries

Measured against live records, not assumed:

| account | `referred_by_wallet` | `access.invited_by_wallet` | gate |
|---|---|---|---|
| pre-deposit era | set | — | passes |
| invited by a wallet | — | set, `inviter_kind: user` | passes |
| created on mainnet | set | — | passes |

The last row used to read **null / null / refused**. A mainnet account came back
from `/v1/aura/wallet/<pubkey>` with every attribution field null while
app.bulk.trade showed "You were referred by &lt;code&gt;" for that same wallet,
because the site read a key-gated table the public endpoint did not join
against. Two things existed only because of that: a `REFERRAL_API_KEY` branch
that queried the key-gated table, and a compiled-in list naming 46 individual
accounts that the gate would admit without asking.

BULK has since populated the public record, and both are gone. Measured before
removing them: all 46 listed wallets return `referred_by_wallet` and
`referred_by_code` naming the same owner, and with the list emptied in a live
process the indexer alone admitted the same 46 and still refused a wallet that
had signed up elsewhere.

### What the gate is now

One question, asked at startup, for every account without exception:

> does BULK's public index say this wallet arrived through the owner?

`SEALED_WALLETS` and `SEALED_CODES` hold the owner's identity — one referrer
wallet, one referral code, as sha256 digests — and the indexer's answer is
matched against them by three routes: `referred_by_wallet`, `invited_by_wallet`
(one address covers both, and keeps covering invitees as codes are consumed and
reissued), and `referred_by_code`.

There is no fourth route and no list of admitted accounts. Consequences, all
intended:

- **Nothing ships when someone new signs up.** They sign up through the link and
  the next start works.
- **The owner's own referral wallet does not pass**, because nobody referred it.
  Its trading accounts do — those signed up through the link like everyone else.
- **While the index is unreachable, nothing runs.** `allow_on_error` is forced
  off in a sealed build. The gate blocks startup only, so this can never
  interrupt a cycle already under way or strand an open position.

`invited_by_code_id` is deliberately not a route: it is an internal id
(`INV-4-004748`), not the `BULK-XXX-XXX` code an owner can see and share, so a
list of it could never be populated from what an owner holds.

When `SEALED_WALLETS` is non-empty the build is *sealed*: the `access` block in
`settings.yaml` is ignored in full, and `check_access` is called unconditionally
rather than behind a config flag. Every field that block used to have was a way
around the check:

| field | the bypass |
|---|---|
| `wallets`, `codes` | add an allowed referrer |
| `require_referral: false` | switch the gate off |
| `allow_on_error: true` | set it, then unplug the network |

A gate its own config file can open is not a gate, so a sealed build reads none
of them. A fork that empties `SEALED_WALLETS` gets the configurable gate back.

### What it does not do

**It does not stop anyone who edits the source.** This ships as Python. The
check is one `return True` from gone, and no amount of hashing changes that —
the digests are compared by code the same person can rewrite. Freezing it into
an executable moves that edit rather than preventing it.

**The digests hide the address from a reader, not from a search.** sha256 of an
address is irreversible in general, but the set of BULK accounts is small and
largely enumerable, so someone willing to hash every known account can match
one. What hashing buys is that the value is not sitting in the file for anyone
who opens it.

So the honest claim is narrow: it raises the cost of bypassing from *editing a
line in a config file* to *reading and patching the program*. That is the
ceiling for any check that runs on the machine it is meant to restrict.

Enforcement would have to be server-side, and it would have to gate something
the bot cannot compute for itself. This bot signs with the operator's own key
and talks straight to BULK, so there is nothing in its path to withhold — which
is why the gate is where it is, and why it claims no more than it does.
