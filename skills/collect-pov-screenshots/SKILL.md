---
name: collect-pov-screenshots
description: Log into a website using credentials stored in the macOS keychain, then optionally spin up parallel headless workers (dashboard / risk / governance agents) that reuse the authenticated session to capture full-page screenshots. Retrieves password and TOTP secret from two keychain entries via `security find-generic-password`, generates a current TOTP code, authenticates, and saves Playwright storage_state so capture workers can run silently in parallel. Trigger when the user asks to "log in to <site>", "sign into <site>", "capture screenshots of <area>", or references this skill by name.
---

# collect-pov-screenshots

Authenticate to a website using credentials kept in the macOS login keychain.

## Inputs

Before doing anything else, confirm you have all three of these from the user. Ask for whichever are missing:

1. **URL** — the page to log in to (e.g. `https://pov-f.vezacloud.com`).
2. **Account identifier** — the value used to log in *and* the `-s` (service) field on the keychain entries. Typically an email address (e.g. `alice@example.com`).
3. **Prefix** — a short tag distinguishing this credential set from others on the same account (e.g. `pov-f`, `jira-prod`). Used as the `-a` (account) suffix on the keychain entries.

Given those, the skill expects two keychain entries (the account identifier itself is the username, so no separate `username` entry is needed):

| Purpose     | `-a` (account)        | `-s` (service)         |
| ----------- | --------------------- | ---------------------- |
| Password    | `<prefix>-password`   | `<account-identifier>` |
| TOTP secret | `<prefix>-totp`       | `<account-identifier>` |

If the entries do not exist, tell the user how to add them (see "Storing credentials" below) before continuing.

## Retrieving credentials

**Critical:** never run `security ... -w` in a way that lets the secret reach stdout (don't run it bare, don't pipe it through `tee`, don't `echo` the variable). Always capture into a shell variable in the same command, and verify success by exit code and length — never by printing the value.

```bash
PASSWORD=$(security find-generic-password -a "<prefix>-password" -s "<account>" -w 2>/dev/null) \
  || { echo "MISSING:password"; exit 1; }
TOTP_SECRET=$(security find-generic-password -a "<prefix>-totp" -s "<account>" -w 2>/dev/null) \
  || { echo "MISSING:totp"; exit 1; }

# Verify without revealing
[ -n "$PASSWORD" ]    && echo "password:OK(len=${#PASSWORD})"       || echo "password:EMPTY"
[ -n "$TOTP_SECRET" ] && echo "totp_secret:OK(len=${#TOTP_SECRET})" || echo "totp_secret:EMPTY"
```

The first invocation may trigger a macOS keychain access dialog. If lookups appear to silently fail under suppressed stderr, re-run **one** lookup unsuppressed so the dialog (or the real error) becomes visible — but only after redirecting stdout to `/dev/null` so the secret cannot leak if access is granted:

```bash
security find-generic-password -a "<prefix>-password" -s "<account>" -w >/dev/null
```

If any lookup returns a non-zero exit code, report which entry is missing and stop. Do not fall back to prompting unless the user asks.

## Generating the TOTP code

Pipe the secret into the bundled script — it uses only the Python stdlib, so no extra installs are required:

```bash
TOTP_CODE=$(printf '%s' "$TOTP_SECRET" | ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/totp.py)
```

The script accepts the secret on stdin (preferred — keeps it out of `ps`) or as `argv[1]`. It assumes the standard TOTP defaults: 6 digits, 30-second period, SHA-1. If a site uses different parameters, update the call site.

## Storing credentials (one-time setup)

If the user needs to add entries, give them these commands. The `-w` with no value triggers a hidden prompt, so the secret never lands in shell history. Add `-U` to update an existing entry in place.

```bash
security add-generic-password -a "<prefix>-password" -s "<account>" -w
security add-generic-password -a "<prefix>-totp"     -s "<account>" -w
```

For the TOTP entry, paste the base32 secret shown when the site offered "set up authenticator app" (often behind a "can't scan QR?" link).

To rotate or replace a value, either pass `-U` to the same command, or delete first:

```bash
security delete-generic-password -a "<prefix>-password" -s "<account>"
```

## Logging into the website

**Always run the full login flow at the start of every skill invocation, even if `state/storage_state.json` already exists from a previous run.** Do not check for, reuse, or skip past an existing storage_state file — overwrite it with a fresh authenticated session each time. The session cookies in `storage_state.json` can silently expire or be invalidated server-side, and a stale state file is indistinguishable from a fresh one until a worker actually hits a login redirect. Re-authenticating up front is cheap (a few seconds) and makes the capture step reliable.

Use the bundled Playwright driver: [`scripts/login.py`](scripts/login.py). It reads credentials from environment variables (never argv, so they don't appear in `ps`), launches a **headless** Chromium by default, and handles both single-page and multi-step (username → password → TOTP) login flows using selector heuristics. When the heuristics can't find a field, the script exits non-zero so the failure is loud. Re-run with `LOGIN_HEADED=1` to launch a visible browser and have it pause on `page.pause()` for manual completion.

### Prerequisites

- `python3` with `playwright` installed (`pip install playwright` or `pip3 install playwright`).
- Chromium downloaded: `python3 -m playwright install chromium` (one-time, ~170 MB).

### Invocation

Combine the keychain lookup, TOTP generation, and the driver into a single shell invocation. Secrets stay in shell variables, get exported into the child's environment, and never reach the command line:

```bash
ACCOUNT="<account-identifier>"
URL="<login-url>"

LOGIN_PASSWORD=$(security find-generic-password -a "<prefix>-password" -s "$ACCOUNT" -w 2>/dev/null) \
  || { echo "MISSING:password"; exit 1; }
TOTP_SECRET=$(security find-generic-password -a "<prefix>-totp" -s "$ACCOUNT" -w 2>/dev/null) \
  || { echo "MISSING:totp"; exit 1; }
LOGIN_TOTP=$(printf '%s' "$TOTP_SECRET" | ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/totp.py)
unset TOTP_SECRET

export LOGIN_URL="$URL"
export LOGIN_USERNAME="$ACCOUNT"
export LOGIN_PASSWORD
export LOGIN_TOTP
# Set this only when you intend to launch screenshot workers afterwards:
export LOGIN_STORAGE_STATE="$HOME/.claude/skills/collect-pov-screenshots/state/storage_state.json"

python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/login.py
```

The script runs headless and exits as soon as `storage_state.json` is written, so the next step (capture workers) can run immediately. Set `LOGIN_HEADED=1` only when you need to watch the flow or finish a step the heuristics can't handle.

### Script output

The driver prints one `step=...` line per stage (`username`, `username_submit`, `password`, `password_submit`, `totp`, `totp_submit`), plus a final `final_url=...` line so you can confirm you ended up off the login page. It never prints credential values.

## Parallel screenshot capture (dashboard / risk / governance)

After a successful login that exported `LOGIN_STORAGE_STATE`, the bundled
[`scripts/capture.py`](scripts/capture.py) can spin up headless Playwright
workers — one per named "agent" — that reuse the saved session and take
full-page screenshots of a configured list of URLs.

The areas and URLs come from [`config.json`](config.json) at the skill root:

```json
{
  "site": "pov-f",
  "storage_state_path": "~/.povreadout/state/storage_state.json",
  "screenshot_dir":     "~/.povreadout/screenshots",
  "viewport": { "width": 1920, "height": 1080 },
  "agents": {
    "dashboard":  { "urls": [ { "name": "main", "url": "https://.../dashboard" } ] },
    "risk":       { "urls": [ { "name": "main", "url": "https://.../risk" } ] },
    "governance": { "urls": [ { "name": "main", "url": "https://.../governance" } ] }
  }
}
```

The top-level `site` key is required and acts as a namespace under
`screenshot_dir` so captures from multiple sites do not collide. Each entry
under `urls` is captured as one PNG named after its `name` (slugified) and
written to `<screenshot_dir>/<site>/<agent>/<name>.png`. Use a short,
filesystem-safe value (e.g. the same prefix used for the keychain entries:
`pov-f`, `jira-prod`). Override per-invocation with `--site <name>` on
`capture.py`.

When you point this skill at a new site, change `site` in `config.json`
(and the URLs under `agents`) before running the workers — otherwise the new
captures will be written under the previous site's folder.

### Running the workers

Immediately after the login step above completes (which always runs — see
"Logging into the website"), launch every worker in parallel — the
URL-based `capture.py --agent <name>` workers, one
`query_builder_capture.py --pair <name>` worker per pair in
`config.query_builder.pairs`, *and* one `graph_capture.py --pair <name>`
worker per pair in `config.query_builder.graph_pairs`. They are all
independent processes, so the cleanest path is one concurrent `Bash` tool
call per worker, all sent in a single message.

Before launching, **read `config.json`** to enumerate the keys under
`agents`, the `name` of each entry under `query_builder.pairs`, and the
`name` of each entry under `query_builder.graph_pairs`; do not hard-code
the list, because new agents/pairs are added to config without changing
this doc. With the current config, the parallel batch looks like:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/capture.py --agent dashboard
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/capture.py --agent risk
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/capture.py --agent governance
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py --pair azure-ad-license-by-user
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py --pair azure-ad-license-by-dormant-user
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/graph_capture.py --pair azure-ad-license-by-user
```

If you want isolated context windows (e.g. to summarise findings per area),
use `Agent` tool calls with `subagent_type=general-purpose` in a single
message instead — one per worker — each instructing the subagent to run the
corresponding command and report what it captured.

Each worker prints one line per URL:

```
site=pov-f agent=dashboard url=https://.../dashboard status=ok file=.../pov-f/dashboard/main.png
site=pov-f agent=dashboard done ok=1 fail=0 out=.../screenshots/pov-f/dashboard
```

### Adding a new area

Add a new key under `agents` in `config.json` with its own `urls` list, then
invoke `capture.py --agent <new-name>`. No code changes required.

## Query Builder captures (entity-type → relates-to pairs)

For screenshots of the Veza Query Builder showing a specific entity-pair
mapping (e.g. Azure AD License × Azure AD User), use
[`scripts/query_builder_capture.py`](scripts/query_builder_capture.py). The
script reuses `storage_state.json`, drives both dropdowns, and waits for the
`query_spec:nodes_async_get` API response (status `DONE`) before screenshotting
— a far more reliable wait condition than `networkidle` on this page.

Pairs live under `query_builder` in `config.json` and use the *internal*
node-type identifiers from the API payload (no spaces). Copy these straight
out of a DevTools HAR — they look like `AzureADLicense`, `AzureADUser`,
`OktaUser`, `OktaGroup`, etc.

```json
"query_builder": {
  "url": "https://pov-f.vezacloud.com/app/query-builder",
  "pairs": [
    { "name": "azure-ad-license-by-user", "entity_type": "AzureADLicense", "relates_to": "AzureADUser" }
  ]
}
```

Run all pairs, one pair, or an ad-hoc pair without editing config:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py --pair azure-ad-license-by-user
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py \
    --entity-type AzureADLicense --relates-to AzureADUser --name license-by-user
```

Output goes to `<screenshot_dir>/<site>/query-builder/<pair-name>.png`.

### Running pairs as parallel workers

Each pair is a self-contained capture, so the cleanest way to run many of
them is one worker per pair in parallel — the same model used by the
`agents` workers above. After a successful login that exported
`LOGIN_STORAGE_STATE`, fire one `query_builder_capture.py --pair <name>`
process per pair as concurrent `Bash` tool calls in a single message:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/query_builder_capture.py --pair azure-ad-license-by-user
# add one line per additional pair you want to run in parallel
```

Each worker is independent (its own Playwright browser context), and they
all read the same `storage_state.json` — they do not interfere with each
other. To run *all* configured pairs sequentially in one process instead,
invoke the script with no `--pair` flag.

If the dropdown selectors miss (the Veza UI may evolve), set
`QB_DEBUG_DIR=/tmp/qb-debug` to capture a snapshot after every step, or
`QB_HEADED=1` to watch the flow in a visible browser.

### Finding the internal node type names

Open DevTools → Network → run the query in the UI → inspect the request to
`/api/private/assessments/query_spec:nodes_async_create`. The `node_type`
fields under `source_node_types` and `relates_to_exp.specs[].node_types` are
the values to put in `config.json`.

### Adding a filter to a pair

The Query Builder filter UI is non-trivial to automate, so the script
instead supports **network-level filter injection**: it intercepts the
`nodes_async_create` request and rewrites the body to add a
`condition_expression` onto the Relates To node. The right-panel table
renders the filtered rows correctly; the left-panel Filters section will
not show chips/badges because the page UI never knew about the rewrite.
The table title (e.g. "7 Azure AD Licenses" vs "16 Azure AD Licenses") is
the visible proof that filtering happened.

Add an optional `relates_to_conditions` field to a pair. Its value is the
raw `condition_expression` object copy-pasted from a HAR — same shape Veza
sends to its own backend:

```json
{
  "name": "azure-ad-license-by-dormant-user",
  "entity_type": "AzureADLicense",
  "relates_to": "AzureADUser",
  "relates_to_conditions": {
    "operator": "AND",
    "specs": [],
    "tag_specs": [],
    "child_expressions": [
      {
        "operator": "OR",
        "specs": [
          { "property": "is_active", "fn": "EQ", "value": false, "not": false },
          { "property": "last_successful_login_at", "fn": "LT", "value": "$COOKIE_TIMEVAR_90_DAY_AGO", "not": false }
        ],
        "tag_specs": [],
        "child_expressions": []
      }
    ]
  }
}
```

To capture a filter for a new pair: set it up manually in the Veza UI with
DevTools → Network open, run the query, find the
`query_spec:nodes_async_create` request, copy the
`relates_to_exp.specs[0].node_types.nodes[0].condition_expression` value
into your pair config. No code changes needed.

## Query Builder Graph captures (Open in Graph)

For screenshots of the Veza Graph view that opens when you click the
**Open in Graph** button on the Query Builder, use
[`scripts/graph_capture.py`](scripts/graph_capture.py). It reuses the
same Entity Type / Relates To dropdown pipeline as
`query_builder_capture.py`, then clicks `[data-testid="cqb-open-in-pg"]`,
switches to the new tab Veza opens for the graph, waits for the graph to
settle, and writes a full-page PNG.

Wait condition is best-effort: a HAR of a real "Open in Graph" run did
*not* show a dedicated `query_spec` or graph-data fetch (only generic
page-load calls — `/api/private/providers`, `/api/private/system`,
telemetry), so the script falls back to a layered wait — opportunistic
`nodes_async_get` response, then `networkidle`, then a canvas/svg
selector, then a 2.5s settle delay. If a future run reveals a real "graph
ready" endpoint in the network tab, tighten `await_graph_ready()` in the
script to wait for it.

Graph pairs live under `query_builder.graph_pairs` in `config.json` —
same shape as `query_builder.pairs`, kept separate so the table-capture
and graph-capture workers run independently:

```json
"query_builder": {
  "url": "https://pov-f.vezacloud.com/app/query-builder",
  "pairs":       [ /* table captures */ ],
  "graph_pairs": [
    { "name": "azure-ad-license-by-user", "entity_type": "AzureADLicense", "relates_to": "AzureADUser" }
  ]
}
```

Run all graph pairs, one pair, or an ad-hoc pair:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/graph_capture.py
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/graph_capture.py --pair azure-ad-license-by-user
python3 ${CLAUDE_PLUGIN_ROOT}/skills/collect-pov-screenshots/scripts/graph_capture.py \
    --entity-type AzureADLicense --relates-to AzureADUser --name license-by-user
```

Output goes to `<screenshot_dir>/<site>/query-builder-graph/<pair-name>.png`.

`relates_to_conditions` filter injection works the same as for table
pairs — copy a `condition_expression` from a HAR into the pair entry.
Debug envs `QB_HEADED=1` and `QB_DEBUG_DIR=/tmp/qb-debug` also apply,
plus debug snaps from the graph tab are written with a `graph-` prefix.

### Session expiry

Because the login flow runs unconditionally at the start of every skill
invocation, a stale `storage_state.json` should never reach the workers. If
a worker *does* somehow capture a login redirect instead of the real page,
the login step failed silently or completed against the wrong URL — re-run
the skill from the top rather than only re-running the workers.

## Security notes

- Never write credentials to disk or include them in tool-call descriptions, commit messages, or chat output.
- Prefer piping secrets into stdin over passing them as command-line arguments (visible in `ps`).
- Do not log the TOTP code either — it is short-lived but still sensitive.
- `storage_state.json` contains session cookies — treat it like a password.
  The skill writes it `chmod 600` under `state/`; do not check it into git
  or copy it to shared locations.
