# Collaborating on this repository

Four people, twelve rules, **one deployed bot**. The bot that runs on EC2 is a
single artifact, so the work has to converge in one place: this repository.
Sending `.py` files to each other does not work here, for a concrete technical
reason before any process reason:

> This is a **package with relative imports** (`from .client import ...`). A
> single file lifted out of `roostoo/` cannot run and cannot be tested. Sharing
> "just my file" means sharing the whole tree and then reconciling four copies by
> hand.

## The rule: one owner per file

Rule ownership is expressed as **file ownership**, so two people never edit the
same file.

| Owner | Files | Rules |
|---|---|---|
| (you) | `roostoo/universe.py`, `roostoo/strategies/mean_reversion.py` | **1, 2, 3** |
| teammate A | `roostoo/risk.py` — `protective_exits`, `PositionSizer` | 5, 6, 7 |
| teammate B | `roostoo/risk.py` — `RiskManager.evaluate`, `observe` | 8, 9, 10, 11, 12 |
| shared | `roostoo/strategies/base.py`, `roostoo/config.py` | the interface |

`roostoo/risk.py` is the one genuine hotspot: Rules 5–12 all live in it. Two
options, pick one on day one:

1. **One owner for `risk.py`**; the other person reviews and sends parameters.
   Simplest, and fine if the rules are related.
2. **Split it** into `roostoo/rules/stop_loss.py`, `time_stop.py`, `daily_halt.py`,
   `cooldown.py` behind a small `Rule` protocol, so each rule has its own file.
   More upfront work, no conflicts afterwards. Ask and it can be scaffolded.

Everything currently in `risk.py` is a **replaceable default**, not a claim of
authorship: the files were scaffolded so the bot could run end-to-end, and the
teammates who own Rules 4–12 are free to delete and rewrite any of it.

## The two interfaces (freeze these)

Everything else is an implementation detail. Changing either of these needs
agreement from all four people, because everything depends on them:

**1. `Strategy` (`roostoo/strategies/base.py`)** — a strategy is a pure function
of market state:

```python
def generate(self, ctx: MarketContext) -> list[Signal]
```

It never touches the network, never sizes a position, and never decides whether
it is *allowed* to trade. Size belongs to the risk layer; approval belongs to the
engine. That is what lets the identical strategy code run in the backtester and
live.

**2. `RiskManager` (`roostoo/risk.py`)** — the approval gate:

```python
risk.observe(nav, now_ms)                    # day boundary, peak NAV, halts
risk.protective_exits(positions, tickers, now_ms)   # stops, time stops
decision = risk.evaluate(signals, view=..., tickers=..., now_ms=..., bar_idx=...)
risk.record_exit(pair, bar_idx)              # cooldown
```

`evaluate` returns `RiskDecision(approved=[ApprovedAction], rejected=[(pair, reason)])`.
Add a rule by rejecting inside it, not by returning early from the engine.

### Adding a rule that needs strategy data

Pass it on the signal, not by reaching into the strategy:

```python
Signal(pair, ENTER_LONG, reason="...", meta={"atr": atr_value, "z": z_now})
```

The risk layer already reads `signal.meta["atr"]` to place the Rule 5 stop. This
is the seam for anything else a rule needs (realised vol, distance to the mean).

### Adding a whole new strategy

Create `roostoo/strategies/<name>.py`, subclass `Strategy`, and select it with:

```bash
STRATEGY=roostoo.strategies.my_idea:MyStrategy
```

No other file changes, and no conflict with anyone.

## Workflow

```bash
git switch -c rule5-atr-stop          # one branch per rule or per person
# ... edit only the files you own ...
python -m unittest discover -s tests -t .     # must stay green
git add -p && git commit -m "feat(rule5): widen the ATR stop to 3x"
git push -u origin rule5-atr-stop     # then open a Pull Request
```

* **Never commit `.env`.** It is git-ignored; keys live only on the machine that
  runs the bot. If a key is ever committed, rotate it — deleting the commit is
  not enough.
* **CI must pass.** `.github/workflows/ci.yml` runs the 405 unit tests on
  Python 3.10 and 3.13, byte-compiles everything (which is what catches a file
  that does not even parse), and runs the simulator loop and a sample backtest.
  A separate `secret-scan` job scans the history for credentials; it is green,
  but it is *not* yet a required check, so do not rely on it to block a merge.
  The required status check is the single job named `ci`, which gates the
  matrix. A red PR does not get merged.
* **Small, labelled commits.** The competition screens for *Commit History
  Transparency*: the history should show the strategy evolving, and should make
  clear that every trade came from the bot, not from a hand-called API. Keep the
  bot's own trail in `journal/` (git-ignored) and the *code* history in Git.
* **Write the reason in the commit body** when a rule changes. "Widened to 3x
  ATR because 1.5x was hit after 4.2 bars on average and produced a 0% win rate"
  is the kind of evidence a judge can verify against `docs/FINDINGS.md`.

## Windows notes (this repo is developed on Windows, deployed to Linux)

* **PowerShell mangles inline JSON** for native commands: `'{"z_entry": [2.5]}'`
  arrives as `{z_entry: [2.5]}`. Use the `@file` form that `run_sweep.py`
  supports: `python run_sweep.py --grid "@reports/grid_rules.json"`.
* **Line endings are pinned by `.gitattributes`.** A CRLF in
  `deploy/roostoo-bot.service` silently breaks `ExecStart` on Linux, so
  everything is forced to LF. Do not override it.
* The runtime is **stdlib-only on purpose**; do not add numpy/pandas/requests to
  `requirements.txt` without discussing it. The deploy target is a bare EC2 box.
