# bulkdn — delta-neutral bot for BULK

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
> (`mainnet=1, testnet=2, devnet=3`) that the API spec requires in the signature preimage,
> and it has no `SignatureDomain` type at all. Signatures produced by the PyPI build will not
> match what the exchange verifies. The GitHub source is correct.

A second wrinkle: `bulk-client` declares a dependency on `bulk-keychain`, a Rust extension
with no prebuilt wheel for recent Pythons. It is **never imported** anywhere in the SDK, so
it is skipped with `--no-deps`. Building it would otherwise require MSVC build tools.

```bash
python -m venv --system-site-packages .venv

# SDK from source, without the unused Rust dependency
.venv/Scripts/python -m pip install --no-deps \
  "bulk-client @ git+https://github.com/Bulk-trade/bulk-client.git#subdirectory=crates/api-python"

# the SDK's real runtime dependencies
.venv/Scripts/python -m pip install \
  pandas numpy numba websockets pynacl base58 sortedcontainers aiohttp requests

# this project
.venv/Scripts/python -m pip install --no-deps -e .
.venv/Scripts/python -m pip install pytest pytest-asyncio pyyaml
```

Verify the signer is the domain-aware one before trading:

```bash
.venv/Scripts/python -c "from bulk_api.common import SignatureDomain; print(list(SignatureDomain))"
```

If that import fails, you are on the PyPI build and **must not** trade with it.

The master private key comes from the environment, never from the config file:

```bash
export BULK_PRIVATE_KEY=<base58 seed>
```

Then copy and edit the config:

```bash
cp config.example.yaml config.yaml
```

### The sub-account must already exist

This bot does **not** create sub-accounts. The Python SDK cannot sign a `createSubAccount`
action — its signer implements serialization for order and cancel actions only. Create and
fund Sub1 in the BULK UI or with the Rust `bulk-cli`, then put its pubkey in `config.yaml`.

Verify the wiring before trading:

```bash
.venv/Scripts/python -m bulkdn.cli --config config.yaml check
```

This confirms the market specs load and that Sub1 really is a child of your master account.
The bot refuses to run otherwise.

---

## API compliance

Checked against the OpenAPI spec (v3.0.10):

**1. Network-bound signatures.** The domain byte (mainnet 1, testnet 2, devnet 3) is appended to
the signing preimage after `nonce || account`. It is trusted client configuration — never a JSON
field or header — and comes from `network:` in the config, via `SignatureDomain`. The staged
rollout of this byte is long finished: `bulk_client` 0.1.2 always appends it and rejects a
missing `SignatureDomain`, and every live endpoint expects it. The bot no longer probes for a
signature dialect. Verify the SDK with:

```bash
.venv/Scripts/python -c "from bulk_api.common import SignatureDomain; print(list(SignatureDomain))"
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

**Mainnet is live and is the default.** `mainnet-api1.bulk.trade` / `mainnet-ws1.bulk.trade`
serve 20 markets, so `run --live` with no other flags trades real money — `--live` is the only
interlock.

**Testnet is reachable** at `exchange-api.bulk.trade` / `exchange-ws1.bulk.trade` and is fundable
with `bulkdn faucet`, so a full cycle can be rehearsed for free with `--network testnet`. The
host name carries no network qualifier, which has caused it to be mistaken for mainnet before.

**The mainnet WebSocket serves an expired certificate.** Verification fails; the HTTP host is
clean. `ws_ssl_auto_bypass` (on by default) retries without verification, which is what keeps the
account stream connectable.

Test in this order:

**1. Unit tests** — no network, no keys.

```bash
.venv/Scripts/python -m pytest
```

**2. Full-cycle simulation** — no network, no keys, no funds. This is the only way to watch a
complete OPEN → HOLD → EXIT cycle. It pulls real tick/lot/notional rules and live prices from
the public API when reachable, then runs the strategy against an in-process fake exchange.

```bash
.venv/Scripts/python -m bulkdn.cli --config config.yaml simulate
```

Watch `worst |net|` — the largest directional exposure the pair carried, compared against the
**unhedgeable floor**. A hedge smaller than a market's minimum notional cannot be submitted, so
that floor (not zero) is the best neutrality achievable: ~$50 on SOL, ~$1 on BTC. It does not
shrink by trading larger. The run also redelivers every 7th fill to exercise `tradeId` dedup;
`duplicate fills seen` should be non-zero with positions unaffected.

**3. Read-only checks against the live API** — no signing, no orders.

```bash
.venv/Scripts/python -m bulkdn.cli --config config.yaml check    # specs + sub-account wiring
.venv/Scripts/python -m bulkdn.cli --config config.yaml status   # positions and open orders
```

**4. Dry run against mainnet** — connects both account streams and logs every transaction it
*would* send, submitting nothing. Note it cannot advance past OPEN, because nothing fills.

```bash
.venv/Scripts/python -m bulkdn.cli --config config.yaml run
```

**5. Live, minimum size.** Only after the above. Set `hold_minutes: 2` and the smallest sizes
that clear each market's minimum notional, and keep `bulkdn flatten --live` ready in a second
terminal.

## Running

**Orders are not submitted unless you pass `--live`.** Without it the bot connects,
subscribes, runs the full phase machine, and logs every transaction it *would* send.

```bash
# dry run on mainnet — connects and logs, submits nothing
.venv/Scripts/python -m bulkdn.cli --config config.yaml run

# live on testnet — free rehearsal, fund with `bulkdn faucet` first
.venv/Scripts/python -m bulkdn.cli --config config.yaml --network testnet run --live

# live on mainnet — REAL FUNDS (config already defaults to mainnet)
.venv/Scripts/python -m bulkdn.cli --config config.yaml run --live
```

Other commands:

```bash
bulkdn status     # positions, open orders, persisted phase
bulkdn check      # validate config and account wiring
bulkdn faucet     # request testnet funds for both accounts
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

See `config.example.yaml`. The parameters that shape execution:

| Key | Effect |
|---|---|
| `legs.*.size` | Total base size the cycle accumulates |
| `legs.*.offset_bps` | How far inside the touch the resting order sits |
| `legs.*.max_distance_bps` | Drift that triggers a cancel+replace |
| `legs.*.max_order_size` | Cap on any single resting order |
| `hold_minutes` | Time fully open before exiting |
| `chase_interval_s` | How often resting orders are re-evaluated |
| `hedge_tolerance_lots` | Net exposure tolerated before hedging (must be ≥ 1 lot) |
| `overlay_ttl_ms` | How long an unconfirmed fill is trusted before falling back to exchange truth |

The mainnet and testnet URLs both come from the OpenAPI spec's Base URLs. The `staging` entry is
undocumented — note it signs on the **devnet** domain byte despite its host name. Override any of
them with `http_url` / `ws_url` in the config if they differ.

---

## Testing

```bash
.venv/Scripts/python -m pytest
```

The suite covers the pure logic — hedge sizing across partial fills and phase role swaps,
chase thresholds and the orphan-order sweep, tick/lot rounding, and state round-trips. It
needs no network.

Before going live, also run through:

1. `run` (dry-run) on testnet — confirms routing, order-ID computation, and the phase machine.
2. `run --live` on testnet with small sizes and `hold_minutes: 2` — watch a full cycle and
   check net exposure stays within tolerance across partial fills.
3. Kill the process mid-OPEN with a partial fill, restart, confirm it reconciles to ~0.
4. Set `max_net_exposure_usd` very low and confirm it halts and flattens.

---

## Implementation notes

Two things about the SDK shaped this code:

**Sub-account routing.** `BulkWebSocketClient.place_orders` hardcodes
`account = signer.public_key`, so it can only trade the signing account. The wire protocol
is more capable — `account` is who is acted on, `signer` is who authorises, and a master may
sign for its sub-accounts. `RoutedWsClient.submit` in `bulkdn/accounts.py` rebuilds the
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

## Known gaps

- **Dry-run cannot advance past OPEN.** Nothing fills, so positions never move and the entry
  legs never complete. Dry-run verifies connection, subaccount routing, order-ID computation,
  chasing, and the risk checks — it cannot demonstrate a full cycle. Use testnet for that.
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
