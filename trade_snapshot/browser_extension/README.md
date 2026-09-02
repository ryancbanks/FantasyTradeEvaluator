# Fantasy Trade Evaluator browser bridge

This is an unpacked Manifest V3 extension for current Chrome and Edge. It lets the
local Fantasy Trade Evaluator ask the user's normal, installed browser to collect
the same bounded provider evidence as the packaged collector. Google and other
provider sign-in therefore happen in the user's ordinary browser profile rather
than an automation-only browser.

The extension is inert until a loopback app page asks to pair and the user approves
that request in the extension popup. Pairing lasts only for the browser session.

## Load it manually

Chrome:

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Choose **Load unpacked**.
4. Select the `trade_snapshot/browser_extension` directory containing
   `manifest.json`.
5. Pin **Fantasy Trade Evaluator Browser Bridge** so its pairing status is easy to
   inspect.

Edge uses the same steps at `edge://extensions`. Choose **Reload** on the extension
card after changing its source files.

To connect, open Fantasy Trade Evaluator, start its browser-extension connection,
then open the extension popup. Verify the displayed loopback address and the last
four characters of the one-time code before choosing **Pair with app**. Closing the
local app tab, closing the managed scan tab, completing the session, or choosing
**Disconnect** closes the scan tab and removes the session token.

This directory defines only the extension side of the boundary. The local Python
server must expose the endpoints below and is responsible for validating every
returned capture against the existing application schemas.

## Local page bridge

Only a top-level page at `http://127.0.0.1:<port>` or
`http://localhost:<port>` can request pairing. It posts this exact message to its
own origin:

```javascript
window.postMessage({
  source: "fantasy-trade-evaluator-app",
  protocol_version: 1,
  type: "pair.request",
  app_origin: window.location.origin,
  pair_code: oneTimePairCode
}, window.location.origin);
```

The content bridge validates the event source, exact origin, message keys, protocol
version, and code shape before forwarding the request. The popup is the required
user-consent step. The page receives `bridge.ready`, `pair.pending`,
`pair.accepted`, `pair.rejected`, `pair.expired`, and `session.closed` status
messages from source `fantasy-trade-evaluator-extension`; none contains the
session token. A paired page can request a disconnect with:

```javascript
window.postMessage({
  source: "fantasy-trade-evaluator-app",
  protocol_version: 1,
  type: "session.disconnect",
  app_origin: window.location.origin
}, window.location.origin);
```

Pair requests expire after two minutes. A still-valid pending request survives a
Manifest V3 service-worker suspension in trusted session storage, but never a full
browser restart. Only one pending request or active session is accepted at a time.

## Loopback HTTP protocol v1

All requests are JSON `POST`s to the paired `app_origin`. Fetch credentials are
omitted, redirects are rejected, and no URL contains a pair or session token.

### Pair

`POST /api/browser-extension/v1/pair`

```json
{
  "pair_code": "one-time-code",
  "protocol_version": 1,
  "capabilities": ["the exact operation list below"],
  "extension_version": "0.1.0"
}
```

The response is:

```json
{
  "protocol_version": 1,
  "state": "paired",
  "session_token": "separate-session-token",
  "capabilities": ["the exact operation list below"],
  "poll_wait_max_seconds": 20.0
}
```

The extension stores that token only in `chrome.storage.session`, restricted to
trusted extension contexts. It is sent only in the `X-FTE-Extension-Token` header
to the same loopback origin and is cleared on disconnect or browser restart.

### Poll, result, and disconnect

`POST /api/browser-extension/v1/poll` uses body
`{"wait_seconds": 20.0}`. The server returns either
`{"protocol_version":1,"state":"idle"}` or:

```json
{
  "protocol_version": 1,
  "state": "command",
  "command_id": "bounded-unique-id",
  "op": "page.provenance",
  "payload": {}
}
```

Success is posted to `/api/browser-extension/v1/result` as
`{"command_id":"...","result":<JSON value>}`. Failure is
`{"command_id":"...","error":"fixed_error_code"}`. The server acknowledges
with `{"protocol_version":1,"state":"accepted","command_id":"..."}` and rejects
stale IDs. A restored service worker does not replay a claimed command: it first
completes that ID with `worker_restarted_during_command`, then resumes polling.

`POST /api/browser-extension/v1/disconnect` uses `{}` and returns
`{"protocol_version":1,"state":"unpaired"}`.

## Exact capabilities and results

No command can name a script, selector, expression, or source string. The fixed
operation list is:

1. `session.open` — payload `{}` or `{"action_delay_ms":50..5000}`; opens/reuses
   one dedicated tab and returns `{"opened":true}`.
2. `session.navigate` — an allowlisted `url` and optional `timeout_ms`; adds the
   fixed `#fte-scan-v1` marker and returns `{"loaded":true}`.
3. `analyzer.begin` — payload phase `ordinary_power` or `full_playoffs`; records
   intent before navigation and returns `{"ok":true}`.
4. `analyzer.finish` — payload `{}`; discards responses that do not structurally
   match the phase from `analyzer.begin`, waits up to 45 seconds, then returns the
   matching raw analyzer response object. The Python boundary still projects and
   revalidates it before persistence.
5. `analyzer.abort` — payload `{}`; clears any buffered analyzer response and
   returns `{"ok":true}`.
6. `analyzer.bundle` — payload `{}`; returns the unique public bundle as
   `{"url":"https://cdn.fantasypros.com/..."}`.
7. `analyzer.activate_full` — payload `{}`; clicks the unique same-page Full Trade
   Analysis control and returns `{"clicked":true}`.
8. `page.provenance` — payload `{}`; returns only `protocol`, `hostname`, `port`,
   and `pathname` (never query or fragment).
9. `projection.capture` — payload contains the typed `request`, `timeout_ms`, and
   optional `action_delay_ms`. It runs the packaged configuration, three-sample
   stability, scrolling, and pagination loop and returns `{"segments":[...]}`.
10. `ecr.capture` — payload `{}`; returns the packaged `{source,rankings}`
    projection of `ecrData`.
11. `league.capture` — bounded `expected_season`, `expected_week`, and `timeout_ms`;
    returns the packaged `{team_count,sources}` semantic projection.
12. `espn.authenticated_json` — bounded `season`, numeric `league_id`, `timeout_ms`,
    and `maximum_bytes`; constructs exactly the two existing ESPN URLs and returns
    `{league,pro_teams}`.
13. `yahoo.scoring` — payload `{}`; returns `{scoring:"STD"|"HALF"|"PPR"}` or a
    fixed `{error}` from the visible Settings table.
14. `session.wait` — bounded `timeout_ms`; returns `{"ok":true}`.
15. `session.close` — payload `{}` or a fixed reason; acknowledges, closes the scan
    tab, clears the token, and disconnects.

The projection request is exactly `{provider,season,week,horizon,scoring,positions}`.
Providers are `fantasypros`, `espn`, or `yahoo`; weeks are 1–25; horizons are
`weekly` or `ros`; scoring is `STD`, `HALF`, or `PPR`; and positions use the
application's fixed canonical enum.

## Navigation and privacy boundary

Managed navigation is limited to the existing FantasyPros analyzer, ranking, and
projection paths; ESPN's projections path; and Yahoo's generic or numeric-league
Players, Player Search, and Settings paths. Manifest access is limited to those
three providers, the exact ESPN JSON host, and loopback HTTP. There is no
`<all_urls>`, cookie, web-request, debugger, or arbitrary scripting permission.

The fixed scan marker activates the MAIN-world analyzer tap at `document_start`.
It also activates a packaged single-page guard adapted from the existing collector:
`window.open` and `_blank`/`new` links reuse the managed tab. A service-worker guard
closes any child tab that still escapes. Ordinary unmarked provider tabs get no
collector handlers, fetch/XHR patches, or click interception.

The ESPN operation asks the ESPN page itself to make two exact credentialed reads.
The browser attaches its session internally; the extension never calls a cookie
API, reads cookie values, reads `localStorage`/`sessionStorage`, or sends those
values to the app. Likewise, provider login pages (including Google) are not in the
manifest. If a provider insists on a separate OAuth popup, sign in in a normal tab
before starting the scan or choose its same-tab sign-in path—the bridge deliberately
closes child tabs to preserve the one-tab guarantee.

Capture results travel only to the explicitly paired loopback origin. Results are
still untrusted provider data: the server must retain its existing schema,
provenance, size, and privacy validation before writing a weekly bundle.

## Static validation

From the repository root:

```powershell
.build-venv\Scripts\python.exe -m unittest discover `
  -s trade_snapshot\browser_extension\tests -p "test_*.py"
```

The checks validate the manifest surface, exact operation list, packaged script
references, marker gating, session-token isolation, and absence of dynamic code or
browser credential/storage reads. JavaScript files should also pass `node --check`.
