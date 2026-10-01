# Secrets: how credentials are handled, and what to do if one leaks

## The rule

**A credential never enters this repository.** The competition requires a
*public* repository; it does not require a public API key, and the two are only
in conflict if someone puts the key in the code.

The code is public. The keys live on the machine that runs the bot, in
`.env`, which is git-ignored (`.gitignore`, first block).

## How the key actually gets to the bot

```bash
# on the EC2 box only -- never in the repo
cd /opt/roostoo-quant-bot
cp .env.example .env
chmod 600 .env
nano .env            # ROOSTOO_API_KEY, ROOSTOO_SECRET_KEY
```

`deploy/roostoo-bot.service` passes `--env /opt/roostoo-quant-bot/.env`; the unit
file itself contains no secrets, and neither does `Environment=`. The template
`.env.example` is committed with **empty** values on purpose and should stay that
way.

If the organisers ever ask for the key "in the repo", that request conflicts with
their own requirement that the repo be public. Send the key through a private
channel instead, or ask them to issue a throwaway key -- never commit the real
one.

## The three guards

| Guard | Where | Catches |
|---|---|---|
| `scripts/scan_secrets.py --staged` | `.pre-commit-config.yaml` | a credential in the blob about to be committed |
| `scripts/scan_secrets.py --all` | `scripts/publish.ps1` step 1 | a credential in filenames, contents **or history** |
| gitleaks | `.github/workflows/ci.yml` (`secret-scan` job) | the same, with an independent rule set and entropy detection |

The first two share one pattern list (`scripts/scan_secrets.py`), so there is a
single place to add a pattern. gitleaks is deliberately a *second, independent*
implementation: if one has a blind spot, the other may not.

`secret-scan` is currently **not** part of the required `ci` check, because it
scans history and history still contains a leaked key (see below). Add it to the
required checks once the purge is done.

If a string is a *published example* rather than a credential, add the exact
value to `ALLOWED_VALUES` in `scripts/scan_secrets.py` and the `[allowlist]`
block in `.gitleaks.toml`, with a comment saying where it is published. Keep that
list short; every entry is a place a real key could hide.

## Incident: the test key committed on 2026-09-30

**What happened.** Commit `09c9674` ("Add files via upload", pushed straight to
`main` through the GitHub web UI) added
`roostoo/strategies/Model Trading Bot.py`, which hardcoded a Roostoo `API_KEY`
and `SECRET_KEY` on lines 122-123. The repository is public. The same file was
also a syntax error (`return` outside a function at line 270), so it broke CI.

**Why the guard missed it.** `scripts/publish.ps1` compared only
`git ls-files` output -- file *names* -- against a regex for `.env`, `*.pem`,
`*.key`, `credentials.json`. A key hardcoded inside an innocuously named `.py`
file was never read. There was also no content scan in CI, and no pre-commit
hook, and branch protection was not enforced.

**Why it mattered even though it was a test key.** The key authenticated against
`mock-api.roostoo.com`, the venue the bot trades on. Anyone holding it could place
orders on the team's account, and the score includes *Commit History
Transparency: no traces of manually called APIs* -- so a stranger's order could
look like a disqualifying manual trade. A leaked key is a reputational and
eligibility problem, not only a financial one.

### If a real key is ever committed: do these in this order

1. **Rotate first.** Ask the issuer for a new key and revoke the old one.
   Deleting the commit does *not* unpublish it -- GitHub's history, forks and
   caches keep it, and so does every clone.
2. **Purge the history**, then force-push. `git-filter-repo` is the maintainable
   tool (BFG also works):
   ```bash
   pip install git-filter-repo
   # remove the file from every commit
   git filter-repo --invert-paths --path "roostoo/strategies/Model Trading Bot.py"
   # or scrub a literal string wherever it appears (do not paste the key into a
   # file that is itself committed -- pass it in from the environment)
   printf '%s==>REMOVED\n' "$LEAKED_KEY" > /tmp/replacements.txt
   git filter-repo --replace-text /tmp/replacements.txt
   git push --force-with-lease origin main
   ```
3. **Tell every collaborator to re-clone.** Their existing clones still contain
   the secret and will re-introduce it on their next push.
4. **Check GitHub**: Settings -> Code security -> Secret scanning and Push
   protection should both be on. Push protection blocks the *next* one at push
   time, before it ever lands.
5. **Then** turn on the branch ruleset.

> **Ordering matters, and it is easy to get backwards.** The `rules for
> collaboration` ruleset (id `24124644`) includes `non_fast_forward`, which
> blocks force-pushes to `main`. Purging history *requires* a force-push. So
> **purge before you enable the ruleset**, or you will have to disable it again
> (or use its bypass list) to finish the cleanup.

## Enabling branch protection

The ruleset is currently `enforcement: disabled` and `main` is protected only by
a classic rule with an **empty** required-status-checks list, so direct pushes
have been possible. Before enabling it:

* the required status check must be `ci`. `ci.yml` provides a gate job named
  exactly `ci` for this reason: a matrix job reports as `test (3.10)` /
  `test (3.13)`, which would never satisfy a rule requiring `ci`, and every PR
  would hang on "Expected -- waiting for status to be reported".
* `main` must be green, or the first PR after enabling it is blocked.
* check the bypass list. If admins are allowed to bypass, the rule will not stop
  the person most likely to push directly.

Then, for a change to the strategy or the risk layer, the flow is: branch -> PR
-> `ci` green -> one approving review -> merge. `CONTRIBUTING.md` describes this;
it is what keeps the bot on `main` runnable during a 14-day scored window.
