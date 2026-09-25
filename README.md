# BULK delta-neutral bot

Runs hedged cycles across a pool of BULK accounts: every master key you give it
and every sub-account under each of them. Each cycle draws a group from the pool
-- one account opens a position with a resting limit order, and one or more
others take the opposite side of every fill with a market order -- so the group
stays close to market-neutral while it trades. It trades every market switched
on in `settings.yaml`; the defaults are BTC-USD and ETH-USD.

> **Mainnet only. This trades real money.** There is no test network to
> rehearse on. Start with the smallest sizes that the exchange accepts.

---

## Install

1. **Install 64-bit Python 3.12 or newer** from [python.org](https://www.python.org/downloads/).
   Tick **"Add python.exe to PATH"** in the installer — without it nothing else works.
   `install.bat` checks the version and stops with an explanation if it is older.
2. **Install git** from [git-scm.com](https://git-scm.com/downloads), keeping every
   default. `update.bat` needs it, and `install.bat` checks for it before it
   starts. The BULK library itself ships with the bot, in `app/vendor`, so the
   install no longer waits on GitHub to answer.
3. **Get the code.** Unzip the archive you were sent, or
   `git clone https://github.com/MakerBuild/Bulk_trading_bot.git` — they come to
   the same thing, because the zip is a clone. `update.bat` works either way.
4. **Double-click `install.bat`.** It builds a local environment, installs
   everything, and checks that transaction signing works. Safe to re-run.

Never done this before? **[ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md](ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md)** walks through it
step by step, in Russian, including what to do when something fails.

---

## Set up

**1. Your private keys**

`install.bat` creates `private_key.local`. Open it and paste your BULK master
account's base58 key below the comment lines — one key per line, nothing else.
Several master keys go on several lines; in `multi` mode every one of them
joins the pool. Order matters a little: the first key is the one the
command-line `transfer` and `create-subaccount` act on by default, and
`single_master` picks a key by its line number.

The file never leaves your machine. Once the bot starts, encrypt it from
**Accounts Management → Encrypt Private Key**; after that the keys are stored
encrypted and you enter a password at startup. All keys go into one encrypted
file, so paste every key you want before encrypting — an encrypted file cannot
be edited by hand.

You only need master keys. A sub-account has no key of its own — it is
created and signed for by its master, and the bot finds every one of them
from the master at startup. Nothing about sub-accounts goes in the settings.

**2. Your settings**

Open `settings.yaml`. Everything you normally change is in the first half:
which markets, which accounts (`mode`), how many groups at once, how much per
cycle, leverage, how long to hold, when to stop, and the safety limits. Each
option says what it does.

Sizes are in dollars -- `notional_usd: 100` is $100 of whatever `symbol` names,
converted to a quantity at the current price when the bot starts. The size and
the hold can be ranges, `notional_usd: 2000-6000`, `hold_minutes: 0.5-1`, drawn
fresh each cycle.

A misspelled setting is not silently ignored: the log warns about it and names
the nearest real one. A value that is not a number where a number belongs, or
not true/false where a switch belongs, stops the bot with the setting's name.

**3. Accounts with money in them**

A BULK account is created by depositing USDC on BULK itself — the bot cannot do
that for you. Every master key needs at least one sub-account. Once a master
has a balance, create one from **Accounts Management → Create New Subaccount**
and move margin to it from **Balance All Subaccounts**, which evens out margin
inside each master's own tree (a transfer cannot cross from one master to
another).

---

## Which accounts trade: `mode`

```
single   one master and its own sub-accounts (single_master: which key, by line)
multi    every master and every sub-account, in one pool     (the default)
```

Both modes trade every market switched on; they differ only in who can be on
the other side of a trade. With more than one master in play, a group's maker
comes from one master's tree and its hedgers from another's. With one key in
the file the two modes are the same thing.
Switch it from **Configuration → Markets & Accounts → Accounts**.

Each cycle a group is drawn from the pool: one account that opens, and up to
`pool.max_takers` accounts that share its hedge. Up to `pool.max_groups` groups
trade at once. An account is only ever in one group, because the hedge is
worked out from positions and one position cannot serve two groups' sums.

---

## Run

**Double-click `run.bat`.** That opens the menu:

```
1. Start                  begin trading
2. Active Strategy        what is open right now
3. History                past fills
4. Accounts Management    create/balance sub-accounts, collect to master, encrypt key, erase local data
5. Configuration          targets, markets & accounts, progress
6. Close All Positions    cancel everything and flatten
7. Logs                   the end of logs.txt, and where the file is
0. Exit
```

Nothing is submitted until you choose **Start** and confirm live trading.
Ctrl+C at any prompt cancels that action — nothing is saved or sent.

To check the setup without trading anything, open a command window in the
bot's folder and type:

```
run.bat check
```

If something goes wrong mid-run, **Close All Positions** cancels every order and
closes every account's positions, reduce-only.

---

## What it does

```
OPEN    the group's maker rests a limit order; every fill is hedged at market
        on the group's other account(s), in fixed shares
HOLD    stays open for hold_minutes, keeping the group balanced
EXIT    the maker closes with a limit order, each fill hedged the same way
```

Then the group disbands and its accounts go back to the pool. The next group
is drawn afresh: accounts, market, size and resting offset.

The run ends when one of your limits in `execution_target` is reached — a number
of cycles (counted as groups started), an amount spent on fees, or an amount
of volume. Fees and volume are counted from fills since the run started.

The bot stops itself if a safety limit in `risk` is breached: too much
one-sided exposure (in total and per group), a position that grew too large,
repeated rejected orders, or a dead market-data connection. That cancels every
order and closes every account.

**One thing worth knowing:** every market sets a minimum order -- $1 on BTC-USD,
$50 on ETH-USD and SOL-USD. A hedge smaller than the floor cannot be sent at
all, so a group can carry up to that much one-sided exposure that no hedge will
remove. That is set by the exchange, not something the bot can tune away.

---

## Notifications (optional)

The bot runs for hours unattended and can stop itself. To hear about it, put a
Telegram bot token and your numeric id in `settings.yaml` under `telegram` —
token from [@BotFather](https://t.me/BotFather), id from
[@userinfobot](https://t.me/userinfobot). It then reports the start, each cycle,
any stop, and the final summary. Messages are sent in the background, so a slow
Telegram never holds up trading. An http(s) proxy in `proxy.local` is used for
them too; a SOCKS proxy is not, and the log says so.

---

## If it will not start

**"Not installed yet"** — run `install.bat` first.

**"This needs 64-bit Python 3.12 or newer"** — install a newer Python, delete
`app\.venv`, run `install.bat` again.

**"no BULK account exists"** — that master has no account on BULK yet. Deposit
USDC on BULK to create one.

**"master ... has no sub-account"** — every key in `private_key.local` needs at
least one. Create it from **Accounts Management → Create New Subaccount**.

**"access denied"** — this build is limited to accounts that signed up through
its owner's referral or invite.

**"is not saved as UTF-8"** — `settings.yaml` was saved in the Windows code page.
Open it in Notepad, **Save As**, Encoding **UTF-8**.

---

## Files

| | |
|---|---|
| `ГАЙД_ПЕРЕД_ПЕРВЫМ_ЗАПУСКОМ.md` | start here: every step, in Russian |
| `install.bat` | one-time setup |
| `update.bat` | fetch the latest version; leaves your settings and keys alone |
| `run.bat` | start the bot |
| `install.sh`, `update.sh`, `run.sh` | the same three on Linux (Ubuntu 24.04) |
| `service.sh` | Linux: keep Telegram control running across reboots (systemd) |
| `settings.yaml` | everything you can change |
| `private_key.local` | your master key(s), one per line |
| `proxy.local` | optional proxy, if BULK is blocked where you are |
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
