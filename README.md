# BULK delta-neutral bot

Runs a hedged two-market cycle across your BULK master account and one of its
sub-accounts. Which two markets is up to you; the defaults are BTC-USD and
ETH-USD. Every fill on one side is immediately offset on the other, so the
pair stays close to market-neutral while it trades.

> **Mainnet only. This trades real money.** There is no test network to
> rehearse on. Start with the smallest sizes that the exchange accepts.

---

## Install

1. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/).
   Tick **"Add python.exe to PATH"** in the installer — without it nothing else works.
2. **Install git** from [git-scm.com](https://git-scm.com/downloads), keeping every
   default. `update.bat` needs it. The BULK library itself ships with the bot,
   in `app/vendor`, so the install no longer waits on GitHub to answer.
3. **Get the code.** Unzip the archive you were sent, or
   `git clone https://github.com/MakerBuild/Bulk_trading_bot.git` — they come to
   the same thing, because the zip is a clone. `update.bat` works either way.
4. **Double-click `install.bat`.** It builds a local environment, installs
   everything, and checks that transaction signing works. Safe to re-run.

Never done this before? **[ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md](ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md)** walks through it
step by step, in Russian, including what to do when something fails.

---

## Set up

**1. Your private key**

`install.bat` creates `private_key.local`. Open it and paste your BULK master
account's base58 key on the last line — one line, nothing else.

The file never leaves your machine. Once the bot starts, encrypt it from
**Accounts Management → Encrypt Private Key**; after that the key is stored
encrypted and you enter a password at startup.

You only need the master key. A sub-account has no key of its own — it is
created and signed for by the master, and the bot finds it automatically.

**2. Your settings**

Open `settings.yaml`. Everything you normally change is in the first half:
which pairs, how much per cycle, leverage, how long to hold, when to stop, and
the safety limits. Each option says what it does.

Sizes are in dollars -- `notional_usd: 100` is $100 of whatever `symbol` names,
converted to a quantity at the current price when the bot starts. The hold can
be a range, `hold_minutes: 0.5-1`, drawn fresh each cycle.

**3. An account with money in it**

A BULK account is created by depositing USDC on BULK itself — the bot cannot do
that for you. Once the master has a balance, create a sub-account from
**Accounts Management → Create New Subaccount** and move some margin to it from
**Balance Subaccounts**.

---

## Run

**Double-click `run.bat`.** That opens the menu:

```
1. Start                  begin trading
2. Active Strategy        what is open right now
3. History                past fills
4. Accounts Management    sub-accounts, balances, collect to master, key, erase local data
5. Configuration          targets and progress
6. Close All Positions    cancel everything and flatten
```

Nothing is submitted until you choose **Start** and confirm live trading.

To check the setup without trading anything:

```
run.bat check
```

If something goes wrong mid-run, **Close All Positions** cancels every order and
closes both accounts.

---

## What it does

```
OPEN    master buys the first   sub-account buys the second
        each fill is hedged on the other account as it happens

HOLD    stays open for hold_minutes, keeping the pair balanced

EXIT    both legs close, each fill hedged the same way
```

It repeats until one of your limits in `execution_target` is reached — a number
of cycles, an amount spent on fees, or an amount of volume.

The bot stops itself and closes everything if a safety limit in `risk` is
breached: too much one-sided exposure, a position that grew too large, repeated
rejected orders, or a dead market-data connection.

**One thing worth knowing:** every market sets a minimum order -- $1 on BTC-USD,
$50 on ETH-USD and SOL-USD. A hedge smaller than the floor cannot be sent at
all, so a pair can carry up to that much one-sided exposure that no hedge will
remove. That is set by the exchange, not something the bot can tune away, which
is why a market with a low floor makes a calmer second leg.

---

## Notifications (optional)

The bot runs for hours unattended and can stop itself. To hear about it, put a
Telegram bot token and your numeric id in `settings.yaml` under `telegram` —
token from [@BotFather](https://t.me/BotFather), id from
[@userinfobot](https://t.me/userinfobot). It then reports the start, each cycle,
any stop, and the final summary.

---

## If it will not start

**"Not installed yet"** — run `install.bat` first.

**"no BULK account exists"** — the master has no account on BULK yet. Deposit
USDC on BULK to create one.

**"master has no sub-account"** — create one from **Accounts Management →
Create New Subaccount**.

**"access denied"** — this build is limited to accounts that signed up through
its owner's referral or invite.

---

## Files

| | |
|---|---|
| `ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md` | start here: every step, in Russian |
| `install.bat` | one-time setup |
| `update.bat` | fetch the latest version; leaves your settings and key alone |
| `run.bat` | start the bot |
| `settings.yaml` | everything you can change |
| `private_key.local` | your key |
| `logs.txt` | everything the bot has done; appears on the first run |
| `app/dev/release.bat` | build a zip to hand out, from a fresh clone rather than the folder |
| `app/vendor/` | the BULK library, so the install does not depend on GitHub |
| `app/docs/DESIGN.md` | how it works internally |

Everything else lives in `app/`, which you can ignore: the code, the test suite,
the tool settings, the longer guides, the local Python environment, and
`app/state/`, where the bot records what it has open — do not delete anything in
`app/state/` while a cycle is running.

<!-- новая версия -->
<!-- релиз 2 -->
<!-- reliz 3 -->
<!-- reliz 4 -->
