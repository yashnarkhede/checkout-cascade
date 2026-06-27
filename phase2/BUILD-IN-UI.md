# Phase 2 — build in the SuperPlane Web UI

**Why not the CLI:** for the `techWizard` org, CLI v0.26.0 returns `internal server error`
("undefined response type") on every WRITE (apps create, canvas update, drafts create,
secrets create). Reads + discovery work. The platform itself works (the empty app
`golden-mission` exists), so we build in the **web UI** and use the CLI only for discovery.

Build into the existing app **golden-mission** (id `808eccf8-d68a-49f2-a484-095668addbe7`).

## Step 1 — Create the secret (UI)
SuperPlane UI → Secrets → New:
- Name: `anthropic`
- Key: `api_key`  Value: *(your Anthropic API key)*

(Optional) a `slack` secret if you don't want the webhook URL inline — otherwise paste the
webhook directly into the Notify Slack node.

## Step 2 — Build the canvas
Open `golden-mission` → Canvas. Two ways:

### Option A — paste YAML (fastest if the editor has a code/YAML view)
Edit `phase2/canvas.yaml`: replace `REPLACE_CHECKOUT_URL`, `REPLACE_PAYMENT_URL`,
`REPLACE_SLACK_WEBHOOK_URL`. Paste it into the canvas YAML view. Save / Publish.

### Option B — built-in Agent (Build mode)
Open the Agent on the app and paste this:

```
Build a canvas with 6 nodes wired in a line:

1. Trigger "Every Minute" — component: Schedule, every 1 minute.
2. "Fetch Checkout Status" — HTTP GET https://<CHECKOUT_URL>/status, timeout 10s.
3. "Fetch Payment Status" — HTTP GET https://<PAYMENT_URL>/status, timeout 10s.
4. "Incident?" — If, expression: $['Fetch Checkout Status'].data.body.error_rate > 0.2
5. "AI Root Cause" — HTTP POST https://api.anthropic.com/v1/messages, content-type application/json,
   Authorization = Custom Header "x-api-key" from secret anthropic/api_key, header anthropic-version: 2023-06-01,
   JSON body: { "model": "claude-sonnet-4-6", "max_tokens": 600,
     "system": "You are an SRE incident commander. Given checkout-service and payment-service /status JSON, the checkout side only sees timeouts but the real cause is payment-service's slow DB (high db_query_ms p95). Write 3-4 sentences: symptom + real root cause with numbers + one recommended action.",
     "messages": [{"role":"user","content":"checkout: {{ $['Fetch Checkout Status'].data.body }} payment: {{ $['Fetch Payment Status'].data.body }}"}] }
6. "Notify Slack" — HTTP POST <SLACK_WEBHOOK_URL>, content-type application/json,
   JSON body: { "text": ":rotating_light: Incident: {{ $['AI Root Cause'].data.body.content[0].text }}" }

Edges: 1→2 (default), 2→3 (success), 3→4 (success), 4→5 (true channel), 5→6 (success).
```

## Step 3 — Verify & publish
- Confirm no node shows an error (e.g. "integration is required" won't apply here — all Core).
- Publish the canvas.

## Step 4 — Demo
1. Make sure phase-1 loadgen (or manual curls) is driving traffic.
2. Inject the incident: `curl -X POST $PAY/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: $KEY"`.
3. Within ~1 min the schedule fires → checkout error_rate > 0.2 → Claude root-causes it →
   Slack message names the DB as the real cause (despite checkout only reporting "timeout").
4. For a faster demo, add a Manual Run (`start`) trigger to the first HTTP node so you can fire it on demand.

## Inputs still needed from you
- Render public URLs: checkout + payment.
- Anthropic API key (→ the `anthropic` secret).
- Slack Incoming Webhook URL.
