# Design notes

How the bot works and why, for whoever maintains it.
For installing and running it, see the README in the project root.

Everything that is not operator-facing lives under `app/`: the package in `app/bulkdn`,
the suite in `app/tests`, tool settings in `app/dev`, these notes in `app/docs`, the
virtualenv in `app/.venv`, and runtime state in `app/state`. The root holds only
`run.bat`, `install.bat`, `settings.yaml`, `private_key.local` and the README.

Runs a hedged BTC/SOL cycle across a BULK **master account** and one **sub-account**.
Every fill on a resting limit order is immediately offset by a market order on the other
account, so combined exposure stays close to zero throughout entry and exit.

| Account | BTC | SOL |
|---|---|---|
| Master | LONG | SHORT |
| Sub1 | SHORT | LONG |

```
OPEN    master BTC limit long  --every fill-->  sub1 BTC market short
        sub1   SOL limit long  --every fill-->  master SOL market short
HOLD    stay ~neutral for N minutes
EXIT    sub1   BTC limit close --every fill-->  master BTC market close
        master SOL limit close --every fill-->  sub1 SOL market close
```

---

## How hedging actually works

The strategy is stated as *"every fill triggers one opposite market order"*. That is what
happens on the happy path, but it is **not** how it is implemented, and the difference
matters.

BULK's fill messages carry no unique fill ID — only `orderId`, `timestamp`, `size`, and
`price`. So a fill replay after a WebSocket reconnect cannot be deduplicated reliably.
Strict one-hedge-per-event accounting would double-hedge on reconnect and leave naked
exposure on a dropped frame.

Instead, the hedge size is **derived from position state**:

```
net = position[maker_account] + position[taker_account]
if |net| >= tolerance:
    market order of -net on the taker account
```

Fill events are only a low-latency *trigger* to re-evaluate. This is behaviourally
identical on the happy path — a 0.10 BTC partial fill moves net to 0.10 and immediately
produces a 0.10 BTC market hedge — but it is **idempotent**: running it twice is a no-op,
a missed fill is caught by the next trigger or the reconciler, and crash recovery needs no
fill journal at all. On restart the bot reads both accounts, computes net, and corrects it.

The maker/taker roles swap between OPEN and EXIT, which is what lets one rule serve both.

---

## Setup

> **Install the SDK from GitHub, not PyPI.** The published `bulk-client` 0.1.2 wheel is
> behind the repository: its signer omits the trailing signature-domain byte
> (`mainnet=1`) that the API spec requires in the signature preimage,
> and it has no `SignatureDomain` type at all. Signatures produced by the PyPI build will not
> match what the exchange verifies. The GitHub source is correct.
>
> Use `--no-deps`. The package declares `bulk-keychain`, which it never imports and which
> has no wheel for Python 3.13+:
>
> ```bash
> app/.venv/Scripts/python -m pip install --no-deps >   "git+https://github.com/Bulk-trade/bulk-client.git@3a6506e#subdirectory=crates/api-python"
> ```
>
> Pin the commit. Older commits of the same `0.1.2` version parse the account stream's
> margin with the wrong field names (`totalBalance` where the API sends `totalMargin`),
> silently reporting a zero balance, and cap WebSocket frames at 16 MiB where the spec
> allows 64 MiB.

A second wrinkle: `bulk-client` declares a dependency on `bulk-keychain`, a Rust extension
with no prebuilt wheel for recent Pythons. It is **never imported** anywhere in the SDK, so
it is skipped with `--no-deps`. Building it would otherwise require MSVC build tools.

```bash
python -m venv --system-site-packages app/.venv

# SDK from source, without the unused Rust dependency
app/.venv/Scripts/python -m pip install --no-deps \
  "bulk-client @ git+https://github.com/Bulk-trade/bulk-client.git#subdirectory=crates/api-python"

# the SDK's real runtime dependencies
app/.venv/Scripts/python -m pip install \
  pandas numpy numba websockets pynacl base58 sortedcontainers aiohttp requests

# test and config tooling; the bot itself is not installed -- run.bat puts
# app/ on PYTHONPATH, so `python -m bulkdn` finds the package where it lies
app/.venv/Scripts/python -m pip install pytest pytest-asyncio pyyaml ruff
```

Verify the signer is the domain-aware one before trading:

```bash
run.bat --help  # or, for a raw check:
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

The master private key comes from the environment, never from the config file:

```bash
export BULK_PRIVATE_KEY=<base58 seed>
```

Then copy and edit the config:

```bash
```

### The sub-account must already exist

This bot does **not** create sub-accounts. The Python SDK cannot sign a `createSubAccount`
action — its signer implements serialization for order and cancel actions only. Create and
fund Sub1 in the BULK UI or with the Rust `bulk-cli`, then put its pubkey in `settings.yaml`.

Verify the wiring before trading:

```bash
run.bat check
```

This confirms the market specs load and that Sub1 really is a child of your master account.
The bot refuses to run otherwise.

---

## API compliance

Checked against the OpenAPI spec (v3.0.10):

**1. Network-bound signatures.** The domain byte (mainnet = 1) is appended to
the signing preimage after `nonce || account`. It is trusted client configuration — never a JSON
field or header — and comes from `network:` in the config, via `SignatureDomain`. The staged
rollout of this byte is long finished: `bulk_client` 0.1.2 always appends it and rejects a
missing `SignatureDomain`, and every live endpoint expects it. The bot no longer probes for a
signature dialect. Verify the SDK with:

```bash
run.bat --help  # or, for a raw check:
app/.venv/Scripts/python -c "from bulk_api.common import SignatureDomain; print(list(SignatureDomain))"
```

**2. Bounded pagination on `POST /account`.** Not applicable — the bot uses only current-state
queries (`fullAccount`, `openOrders`), which still return arrays. It reads no history endpoints,
so `limit`/`startSlot`/`endSlot`/`cursor` never come into play.

**3. `tradeId` on fills.** Now used to deduplicate replayed fills. Note the SDK's WebSocket `Fill`
class **does not carry the field** — it is parsed in `messages/history.py` but dropped by
`messages/trade.py`. `RoutedWsClient._handle_fill` recovers it from the raw payload.

Deduplication is scoped **per account**, because the changelog states maker, taker, and
isolated-account views of one execution share a trade id. A global set would discard the second
account's legitimate view of a trade that crossed between the master and the sub-account.

Trade ids improve the position book but do not replace position-derived hedging: an id only helps
with fills that *arrive*, and says nothing about fills missed, reordered, or occurring while the
process is dead.

## Testing it

**This bot is mainnet-only.** `mainnet-api1.bulk.trade` / `mainnet-ws1.bulk.trade` are pinned
in `app/bulkdn/config.py` together with the signature domain byte, and there is no network selector.
Every `run --live` trades real money, and `--live` is the only interlock.

**There is no rehearsal environment.** Dry-run (`run` without `--live`) is the only way to
exercise the bot without spending, and it cannot advance past OPEN because nothing fills.

**The mainnet WebSocket serves an expired certificate.** Verification fails; the HTTP host is
clean. `ws_ssl_auto_bypass` (on by default) retries without verification, which is what keeps the
account stream connectable.

Test in this order:

**1. Unit tests** — no network, no keys.

```bash
tests
un-tests.bat
```

**2. Full cycle, live, minimum size** — there is no free equivalent. Fund the master on-chain,
`bulkdn transfer` margin to the sub, then run a complete OPEN -> HOLD -> EXIT cycle against the
real matching engine with the smallest sizes that clear each market's minimum notional.

```bash
run.bat run --live
```

Watch net exposure against the **unhedgeable floor**. A hedge smaller than a market's minimum
notional cannot be submitted, so that floor (not zero) is the best neutrality achievable: ~$50
on SOL, ~$1 on BTC. It does not shrink by trading larger.

This is also the only thing that exercises per-action signature correctness, rejections
(`rejectedCrossing`, `cancelledReduceOnly`, `rejectedRiskLimit`), the 25 ms taker speed bump,
the `minNotional` floor, and whether pre-computed order IDs match what the exchange assigns.
Keep `bulkdn flatten --live` ready in a second terminal.

**3. Read-only checks against the live API** — no signing, no orders.

```bash
run.bat check    # specs + sub-account wiring
run.bat status   # positions and open orders
```

**4. Dry run** — connects both account streams and logs every transaction it *would* send,
submitting nothing. Note it cannot advance past OPEN, because nothing fills.

```bash
run.bat run
```

**5. Live, minimum size.** Only after the above. Set `hold_minutes: 2` and the smallest sizes
that clear each market's minimum notional, and keep `bulkdn flatten --live` ready in a second
terminal.

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
| 0. Exit                                    |
+--------------------------------------------+
```

The menu is a front end over the subcommands below, not a second implementation.
Every item that spends money asks for a typed `yes` first — `y` and a bare Enter
both abort — because the bot is mainnet-only and there is no harmless mistake.

Two entries under **Configuration** are deliberately inert. `Total Amount to Burn`
and `Total Trading Volume` are stop conditions the run loop does not read, so the
menu refuses to store them rather than let you set a limit that would never fire.
`History` reports realised volume and fees from the exchange's own fill records in
the meantime.

The subcommands stay available for scripting.

**Orders are not submitted unless you pass `--live`.** Without it the bot connects,
subscribes, runs the full phase machine, and logs every transaction it *would* send.

```bash
# dry run on mainnet — connects and logs, submits nothing
run.bat run

# live — REAL FUNDS, no confirmation prompt
run.bat run --live
```

Other commands:

```bash
bulkdn            # interactive menu (same as `bulkdn menu`)
bulkdn encrypt-key  # encrypt the key file, or change its password
bulkdn status     # positions, open orders, persisted phase
bulkdn check      # validate config and account wiring
bulkdn transfer --to <pubkey> --amount <n>   # fund a sub-account from the master
bulkdn flatten --live   # cancel everything and close all strategy positions
```

`flatten` is the manual panic button. It is reduce-only throughout, so it can never open a
new position in the opposite direction.

---

## Safety

The strategy as originally specified has no halt condition. This implementation adds one,
because the bot fires market orders unattended: if a hedge leg starts failing (insufficient
margin on Sub1, a risk-limit rejection, a dead socket), the position silently stops being
neutral and nothing on the happy path notices.

Any of these cancels all orders, flattens both accounts, and stops:

| Limit | Meaning |
|---|---|
| `max_net_exposure_usd` | Uncorrected directional exposure in either symbol |
| `max_position_usd` | Per-account, per-symbol notional ceiling |
| `max_reject_streak` | Consecutive rejected transactions |
| `ws_stale_timeout_s` | No WebSocket traffic for this long |
| hedge ceiling | A single required hedge above 2× the leg size — means the book and reality have diverged |

Risk checks deliberately use **confirmed** positions only. A limit should not be satisfied
by a fill the exchange has not acknowledged.

### Crash recovery

State is persisted atomically (temp file + `os.replace`) after every transition. On startup
the bot:

1. Reads positions for both accounts over **HTTP** — the WS snapshot has not arrived yet,
   and an empty book is indistinguishable from being flat.
2. Cancels all strategy orders. A resting order that cannot be matched to the recovered
   plan is an unhedged fill waiting to happen.
3. Re-runs the hedge rule to correct inherited exposure.
4. Resumes the persisted phase. Positions with no recorded plan are exited, never added to.

A corrupt state file is fatal rather than ignored — it may be the only record that
positions are open.

---

## Configuration

See `settings.yaml`. The parameters that shape execution:

| Key | Effect |
|---|---|
| `execution_target.cycles` | Cycles to run; 0 is unlimited |
| `execution_target.burn_usd` | Stop once this much has been paid in fees; 0 disables |
| `execution_target.volume_usd` | Stop once this much **qualifying** volume is done; 0 disables |
| `legs.*.leverage` | Max leverage for that market; omit to keep the account's own |
| `risk.max_hedge_impact_bps` | Refuse a hedge whose predicted slippage exceeds this; 0 disables |
| `legs.*.notional_usd` | Dollars the cycle accumulates; converted to a quantity at startup |
| `legs.*.size` | The same, as a base quantity. One or the other per leg, never both |
| `legs.*.offset_bps` | How far inside the touch the resting order sits |
| `legs.*.max_distance_bps` | Drift that triggers a cancel+replace |
| `legs.*.chase_patience_s` | Unfilled for this long: give up the offset and rest on the touch. Still passive |
| `legs.*.tight_distance_bps` | Replace threshold once tightened; follows the touch instead of tolerating drift |
| `legs.*.max_order_notional_usd` | Cap on any single resting order, in dollars |
| `legs.*.max_order_size` | The same cap, as a base quantity |
| `hold_minutes` | Time fully open before exiting; a number or a `low-high` range drawn per cycle |
| `max_phase_minutes` | Cap on OPEN or EXIT; hitting it halts, which cancels and flattens. HOLD is exempt |
| `chase_interval_s` | How often resting orders are re-evaluated |
| `hedge_tolerance_lots` | Net exposure tolerated before hedging (must be ≥ 1 lot) |
| `overlay_ttl_ms` | How long an unconfirmed fill is trusted before falling back to exchange truth |

The mainnet URLs come from the OpenAPI spec's Base URLs and are pinned in `app/bulkdn/config.py`
alongside the domain byte, so the two cannot drift apart. Override them with `http_url` /
`ws_url` in the config only if the published hosts change.

---

## Testing

```bash
tests
un-tests.bat
```

The suite covers the pure logic — hedge sizing across partial fills and phase role swaps,
chase thresholds and the orphan-order sweep, tick/lot rounding, and state round-trips. It
needs no network.

Before going live, also run through:

1. `run` (dry-run) — confirms routing, order-ID computation, and the phase machine.
2. `run --live` with the smallest sizes that clear each market's minimum notional and
   `hold_minutes: 2` — watch a full cycle and check net exposure stays within tolerance
   across partial fills. This spends real money; there is no free equivalent.
3. Kill the process mid-OPEN with a partial fill, restart, confirm it reconciles to ~0.
4. Set `max_net_exposure_usd` very low and confirm it halts and flattens.

---

## Implementation notes

Two things about the SDK shaped this code:

**Sub-account routing.** `BulkWebSocketClient.place_orders` hardcodes
`account = signer.public_key`, so it can only trade the signing account. The wire protocol
is more capable — `account` is who is acted on, `signer` is who authorises, and a master may
sign for its sub-accounts. `RoutedWsClient.submit` in `app/bulkdn/accounts.py` rebuilds the
transaction with both fields set correctly; the signing path itself is untouched.

**Handlers run inside the receive loop.** The SDK awaits event handlers inline in its
WebSocket read loop, and order responses are resolved by that same loop. A handler that
awaited an order submission would block the loop that has to deliver its response, and
deadlock until timeout. So handlers here are synchronous — they update the position book and
signal a worker task, and all order submission happens outside the loop.

Order IDs are computed client-side (a hash over the signed fields), so the bot knows an
order's ID before its response arrives. That is what makes an order placed just before a
crash still recognisable on restart.

---

## The private key

`private_key.local` is git-ignored and holds either a bare base58 seed or an
encrypted envelope. Encrypt it once:

```bash
run.bat encrypt-key
```

```text
Enter password to encrypt privatekeys (empty for default):
Repeat:
```

Argon2id derives a key from the password and XSalsa20-Poly1305 encrypts the
seed under it, both from PyNaCl -- the same library the SDK signs with, so this
adds no dependency. The KDF parameters are stored in the file, so one written
today still opens after they are raised. Poly1305 authenticates the ciphertext,
which is why a wrong password is reported as wrong instead of returning a
plausible-looking key.

The file is written to a temporary name and moved into place, and the envelope
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
position is not a reason to abandon it, or the pair is left directional.

`burn_usd` and `volume_usd` are measured by walking the account tree's fill
history (`POST /account {"type": "fills"}`) and summing what the exchange
recorded, so they survive a restart and cannot drift from what was actually
charged. If that read fails the run continues -- refusing to trade because a
read-only endpoint is down would be worse than overshooting a soft goal by one
cycle.

**`volume_usd` counts qualifying volume only.** The fee documentation states
that "Self-trades between accounts under the same main account do not create
qualifying volume", and this strategy hedges between a master and its own
sub-account, so some fills do cross between them. Those are real spend but earn
no tier credit; a fill whose maker and taker are both inside the tree is
counted toward `burn_usd` and excluded from `volume_usd`. Menu item 5 →
Progress shows the split.

## Leverage

`legs.*.leverage` is applied at startup with `updateUserSettings`, to the
master and to the sub-account separately -- sub-accounts copy the master's
settings when created and are independent afterwards. Only what differs is
sent, and the market's own ceiling from `/exchangeInfo` is checked first.

One symbol per transaction, deliberately: the signed bytes are the map's
entries in iteration order, and a multi-entry map has no order the sender and
receiver are guaranteed to agree on. Note the SDK's own `update_leverage`
sends `m` as a list of pairs where the reference client declares a map, so it
is not used.

## Hedge slippage

`risk.max_hedge_impact_bps` prices a hedge against the market's published
impact curve (`GET /impact`) before sending it, and halts rather than trading
through a market that has become too thin. The hedge size is dictated by the
fill it answers -- shrinking it would leave the pair directional -- so the only
choices are to pay the slippage or to stop.

**The endpoint answers 404 until a curve is published, and no mainnet market
has one at the time of writing.** With no curve there is nothing to check and
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
  traded, so a residual below `lotSize` on either account is expected and treated as flat.
- The bot assumes it is the only thing trading these two symbols on both accounts. Entry
  progress is measured from account positions, so an unrelated position in BTC or SOL would
  be read as strategy fill progress.

- Whether a failing `cx` inside a batch aborts the whole transaction or lets the `l` through
  is not documented. The chaser assumes the worst and sweeps superseded order IDs that are
  still resting.
- The OpenAPI spec documents no rate limits. The chase loop is throttled conservatively.
- Funding costs are not modelled. A delta-neutral pair still pays or receives funding on
  both legs, which is P&L this bot does not account for.

---

## The referral gate, and what it is worth

The bot refuses to start unless the master account signed up through the
owner — by referral code or by redeemed invite, both of which are checked,
because on the wallet this was built for roughly two thirds arrived by invite.

### What the public index actually carries

Measured against live records, not assumed. This is the limit the gate runs
into, and it is worth stating before the mechanism:

| account | `referred_by_wallet` | `access.invited_by_wallet` | gate |
|---|---|---|---|
| pre-deposit era | set | — | passes |
| invited by a wallet | — | set, `inviter_kind: user` | passes |
| created on mainnet | **null** | **null**, `inviter_kind: admin` | **refused** |

A mainnet account comes back from `/v1/aura/wallet/<pubkey>` with every
attribution field null, while app.bulk.trade shows "You were referred by
&lt;code&gt;" for that same wallet. The site reads a different table:

```
GET /v1/aura/mainnet/referrals/<pubkey>  -> 401 missing x-aura-referral-api-key
GET /v1/aura/referrals/traders/<pubkey>  -> 401
GET /v1/aura/access/codes/<pubkey>       -> 401
```

So without a key the gate cannot see a mainnet referral at all, and refuses
accounts that did sign up through the owner. `REFERRAL_API_KEY` in
`referral.py` closes this: when set, a wallet whose public record says nothing
is looked up there before being refused.

One further trap: `access.invited_by_code_id` is an internal id
(`INV-3-031806`), not the `BULK-XXX-XXX` code an owner can see and share, so
the `invite_codes` list can never be populated from what the owner holds.
Match on the inviter's wallet.

The allow-list is compiled into `app/bulkdn/referral.py` as sha256 digests, not
read from `settings.yaml`. When `SEALED_WALLETS` is non-empty the build is
*sealed*: the `access` block in the config is ignored in full, and `check_access`
is called unconditionally rather than behind a config flag.

Every field that block used to have was a way around the check:

| field | the bypass |
|---|---|
| `wallets`, `codes` | add an allowed referrer |
| `invite_codes` | the same, by code |
| `owner_wallets` | an unconditional pass, checked before the indexer |
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
