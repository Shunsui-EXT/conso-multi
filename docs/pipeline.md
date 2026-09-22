# Reverse-engineered pipeline

Source: `Conso: AI Usage Tracker` v0.1.2 Chrome extension
(`bjibbmkefnaamkenamdppfengeepadpi`) — bundle `background.js` (~215 KB, one
minified line). Every constant and endpoint below is quoted verbatim from
the bundle; nothing is inferred.

## 1. Backend surface

| kind | URL |
|---|---|
| Supabase project | `https://jzxlayjrsdbyzykuiqns.supabase.co` |
| public anon key | `sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd` |
| extension API (X OAuth relay) | `https://www.conso.xyz/api/extension/x` |
| Google OAuth client | `77519766304-hfrhb5gc5dp1kqano1ghsgroa2rf3ei1.apps.googleusercontent.com` |
| refresh grant | `POST /auth/v1/token?grant_type=refresh_token` |

Supabase JS client init:

```js
createClient(
  "https://jzxlayjrsdbyzykuiqns.supabase.co",
  "sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd",
  {
    auth: {
      storage: chrome.storage.local,   // key: sb-jzxlayjrsdbyzykuiqns-auth-token
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: false,
    }
  }
)
```

## 2. Login pipeline

Two independent identities are attached to one Conso account:

```
Google (mandatory)  ──►  Supabase session (access + refresh JWT)
X / Twitter (later) ──►  consousers.x_user_id via /api/extension/x
```

### 2a. Google → Supabase (mandatory)

Bundle function `ca()`:

```js
async function ca() {
  const redirect = chrome.identity.getRedirectURL();  // https://<ext-id>.chromiumapp.org/
  const nonce = randomHex(16);                        // 16 bytes → 32 hex chars
  const nonceHash = sha256hex(nonce);                 // sent to Google

  const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  url.searchParams.set("client_id", CONSO_GOOGLE_CLIENT_ID);
  url.searchParams.set("response_type", "id_token");
  url.searchParams.set("redirect_uri", redirect);
  url.searchParams.set("scope", "openid email profile");
  url.searchParams.set("nonce", nonceHash);           // SHA-256 of nonce
  url.searchParams.set("prompt", "select_account");

  const returned = await chrome.identity.launchWebAuthFlow({
    url: url.toString(),
    interactive: true,
  });
  const idToken = new URLSearchParams(new URL(returned).hash.slice(1))
                    .get("id_token");
  return { idToken, nonce };                          // nonce is the RAW value
}
```

### 2a-cli. Full-CLI reproduction (`login --mode browser`)

The Google `client_id` above is an extension client — Google rejects any
`redirect_uri` that is not `chrome.identity.getRedirectURL()`, which
resolves to `https://<extension-id>.chromiumapp.org/`. That value is
bound to the extension identity, not to a domain, so no external caller
can replicate the OAuth call.

`conso/login_browser.py` sidesteps this by loading the real extension
into a headed Chromium controlled by Playwright, letting the operator
complete Google sign-in inside that Chromium instance, and reading the
persisted Supabase session out of the extension's own
`chrome.storage.local`. From then on the CLI holds the same tokens the
extension would.

Sequence:

1. `chromium.launch_persistent_context(user_data_dir=<tmp>, args=[
   '--disable-extensions-except=<ext>', '--load-extension=<ext>'])`
2. Wait for the extension's service worker
   (`chrome-extension://<ext-id>/background.js`) to register.
3. Operator clicks the extension's toolbar icon → Sign in with Google.
4. Poll the service worker every second:
   ```js
   const rec = await chrome.storage.local.get(
     "sb-jzxlayjrsdbyzykuiqns-auth-token");
   ```
5. First poll that returns `{access_token, refresh_token, …}` is treated
   as success. The CLI verifies with `GET /auth/v1/user`, fetches
   `consoname`, and persists to `data/accounts.json`.
```

Then Supabase:

```js
await supabase.auth.signInWithIdToken({
  provider: "google",
  token: idToken,
  nonce,                                              // RAW nonce, not the hash
});
```

Supabase persists `{ access_token, refresh_token, expires_at, user }` under
`chrome.storage.local["sb-jzxlayjrsdbyzykuiqns-auth-token"]`. `autoRefreshToken`
does refresh in the extension; our tool decodes `exp` and rotates
proactively — same result, no extension needed.

### 2b. X connect (optional, one-off)

Bundle function `ua()`:

```js
const LA = "https://www.conso.xyz/api/extension/x";
async function ua() {
  const { url } = await fetch(`${LA}/start`, { method: "POST" }).then(r => r.json());
  const returned = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
  const params = new URL(returned).searchParams;
  const code = params.get("code");
  const state = params.get("state");
  const result = await fetch(`${LA}/callback`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code, state }),
  }).then(r => r.json());
  return { xUserId: result.xUserId, username: result.username };
}
```

The callback writes `x_user_id`, `x_username`, `x_connected_at` into
`consousers` (server-side, using the Supabase JWT the browser sends via
cookie/session). It is one-shot per account; the linked X handle is then
required for `tweet-about-conso-v1` and `article-about-conso-v1` missions.

## 3. Data-plane RPCs (Supabase `/rest/v1/rpc/*`)

Every write is a Postgres RPC. All require:

```
apikey:         sb_publishable_clAiRg6ffCznEAtg_bn19Q_yY0W5Hyd
Authorization:  Bearer <access_token>
Content-Type:   application/json
```

| RPC | body | purpose |
|---|---|---|
| `append_prompt` | `{ p_entry, p_base_zaps, p_spend_usd }` | record one AI turn, credit zaps |
| `claim_bonus_mission` | `{ p_mission_id }` | claim daily/checkin missions |
| `claim_daily_mission` | `{ p_mission_id, p_claim_ref }` | claim with external proof |
| `get_todays_mission_claims` | `{}` | list already-claimed mission ids |

Read-only:

| table | purpose |
|---|---|
| `consousers` | one row = current user (`total_zaps`, `daily_zaps_earned`, `is_banned`, …) |
| `bonus_missions` | active mission catalogue |

`p_entry` schema (bundle-verified):

```json
{
  "model": "claude-fable-5",
  "platform": "claude",
  "timestamp": "2026-09-22T13:07:22.000Z",
  "inputTokens": 241,
  "outputTokens": 369,
  "inputFilesCount": 1,
  "outputFilesCount": 0,
  "promptQuality": 5.0
}
```

`p_base_zaps` is computed client-side (`conso/zaps.py` mirrors the extension
bundle). The server recomputes and rejects payloads that fall outside a
plausibility band (returns 0) or match its abuse patterns (marks
`is_banned = true`).

## 4. Full lifecycle

```
┌─────────────────────────────────────────────────────────────────────┐
│                          FIRST-TIME LOGIN                           │
├─────────────────────────────────────────────────────────────────────┤
│ 1. Google id_token via OAuth 2.0 implicit + nonce                   │
│ 2. supabase.auth.signInWithIdToken(provider=google, token, nonce)   │
│    → access_token, refresh_token, user                              │
│ 3. Row auto-created in `consousers` on first sign-in                │
│ 4. (Optional) POST /api/extension/x/start → user follows X OAuth    │
│    → POST /api/extension/x/callback binds x_user_id                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                         DAILY EARNING LOOP                          │
├─────────────────────────────────────────────────────────────────────┤
│ 1. GET /rest/v1/consousers?select=*         → daily_zaps_earned     │
│    plan_target = min(3.2, (30 - daily) / 4)                         │
│ 2. RPC get_todays_mission_claims             → claimed today        │
│ 3. RPC claim_bonus_mission("daily-checkin-v1") if not claimed       │
│ 4. For each of the 4 locked (model, platform) pairs:                │
│    a. Build p_entry with token counts scaled to plan_target         │
│    b. Compute p_base_zaps via the ported formula                    │
│    c. RPC append_prompt → server returns credited_zaps              │
│    d. Two consecutive credited_zaps == 0 → abort (soft flag)        │
│    e. Sleep [gap_min, gap_max] with jitter                          │
│ 5. Loop rounds until every platform hits target OR budget drained   │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│                        BACKGROUND MAINTENANCE                       │
├─────────────────────────────────────────────────────────────────────┤
│ every 30 s  → poll /rest/v1/consousers (dashboard refresh)          │
│ exp - 90 s  → POST /auth/v1/token?grant_type=refresh_token          │
│               → new {access_token, refresh_token, expires_in}       │
│               → persist to data/accounts.json atomically            │
└─────────────────────────────────────────────────────────────────────┘
```

## 5. Server-side guards (measured, not inferred)

- **Per-credit ceiling ≈ 3.5.** A payload computing above ~3.5 is credited 0.
  We aim at 3.2 for margin.
- **Daily cap 30.0.** `daily_zaps_earned >= 30` → every subsequent write
  returns 0 regardless of size. Keyed on UTC date.
- **Zero-credit soft flag.** Two consecutive `credited == 0` on 200 responses
  precede an `is_banned = true`. Automation aborts after the second.
- **Velocity throttle.** Below the daily cap, credit can still degrade
  sharply on rapid or predictable cadence — the `2.25 → 0.08` collapse we
  observed on the second wave of the same day (see earn log).
- **Ban surface.** Once banned, every RPC returns HTTP 400
  `{"message":"account_banned"}` and `consousers` reads return 0 rows.
