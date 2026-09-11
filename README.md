# BULK delta-neutral bot

Runs a hedged BTC/SOL cycle across your BULK master account and one of its
sub-accounts. Every fill on one side is immediately offset on the other, so the
pair stays close to market-neutral while it trades.

> **Mainnet only. This trades real money.** There is no test network to
> rehearse on. Start with the smallest sizes that the exchange accepts.

---

## Install

1. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/).
   Tick **"Add python.exe to PATH"** in the installer — without it nothing else works.
2. **Install git** from [git-scm.com](https://git-scm.com/downloads), keeping every
   default. The BULK library is fetched from GitHub.
3. **Double-click `install.bat`.** It builds a local environment, installs
   everything, and checks that transaction signing works. Safe to re-run.

Never done this before? **[docs/INSTALL.md](docs/INSTALL.md)** walks through it
step by step, in Russian, including what to do when something fails.

---

## Set up

**1. Your private key**

Rename `private_key.example` to `private_key.local` and paste your BULK master
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
4. Accounts Management    sub-accounts, balances, key encryption
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
OPEN    master buys BTC        sub-account buys SOL
        each fill is hedged on the other account as it happens

HOLD    stays open for hold_minutes, keeping the pair balanced

EXIT    both legs close, each fill hedged the same way
```

It repeats until one of your limits in `execution_target` is reached — a number
of cycles, an amount spent on fees, or an amount of volume.

The bot stops itself and closes everything if a safety limit in `risk` is
breached: too much one-sided exposure, a position that grew too large, repeated
rejected orders, or a dead market-data connection.

**One thing worth knowing:** BULK will not accept a SOL order under $50 or a BTC
order under $1. A hedge smaller than that cannot be sent at all, so the pair can
carry up to about $50 of one-sided SOL exposure that no hedge can remove. That
is a floor set by the exchange, not something the bot can tune away.

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
| `install.bat` | one-time setup |
| `run.bat` | start the bot |
| `settings.yaml` | everything you can change |
| `private_key.local` | your key (create it from `private_key.example`) |
| `docs/DESIGN.md` | how it works internally |

`bulkdn/` is the code, `tests/` the test suite, and `state/` is where the bot
records what it has open — do not delete anything in `state/` while a cycle is
running.
