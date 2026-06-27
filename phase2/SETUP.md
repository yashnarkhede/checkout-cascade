# Phase 2 setup — SuperPlane Incident Commander

Goal: a SuperPlane Cloud app that polls both services' `/status`, detects the incident,
asks Claude for the root cause, and posts it to Slack. Minimal-setup approach: only Core
components + 2 secrets (no typed integrations to OAuth).

## Prerequisites (you do these)

1. **SuperPlane Cloud org** — you need one (from the hackathon team form). Sign in at
   https://app.superplane.com and confirm you can see an organization.

2. **Install the CLI:**
   ```bash
   curl -fsSL https://install.superplane.com/install.sh | sh
   ```

3. **Service account token + connect:** in the SuperPlane UI create a Service Account,
   copy its API token, then:
   ```bash
   superplane connect https://app.superplane.com <API_TOKEN>
   superplane whoami        # should print your identity/org
   ```

4. **Slack Incoming Webhook** — create one (Slack → Apps → Incoming Webhooks → Add to a
   channel) and copy the `https://hooks.slack.com/services/...` URL.

5. **Anthropic API key** — from console.anthropic.com (or swap to Groq/OpenAI; we'll adjust the call).

## Then (we do these together)

6. **Create the two secrets** (exact `secrets create` YAML shape confirmed via CLI):
   - `anthropic_key` = your Anthropic API key
   - `slack_webhook_url` = your Slack webhook URL

7. **Discover schemas** (source of truth, before finalizing the canvas):
   ```bash
   superplane index triggers --name schedule --output json
   superplane index actions  --name http     --output json
   superplane index actions  --name if       --output json
   ```

8. **Edit `canvas.draft.yaml`**: replace `REPLACE_CHECKOUT_URL` / `REPLACE_PAYMENT_URL`
   with your Render public hosts; reconcile any field-name differences from step 7.

9. **Create + verify the app:**
   ```bash
   superplane apps create --canvas-file phase2/canvas.draft.yaml --canvas-auto-layout horizontal
   superplane apps canvas get incident-commander -o yaml   # check: no errorMessage / warningMessage
   ```

10. **Demo:** inject `db_slowdown/6` on payment → within ~1 min the canvas fires →
    Claude root-causes it → Slack message lands naming the DB as the real cause.

## Notes / things to verify against the CLI
- `http` component field names (`json` vs `body`, `headers` shape, `successCodes`, `timeoutSeconds`)
  and whether the JSON response is auto-parsed so `$['...'].data.body.error_rate` /
  `.data.body.content[0].text` work as written. Confirm with `index actions --name http --output json`.
- Secret reference syntax in expressions: `{{ secret('name') }}`.
- If you prefer NATIVE integrations instead of secrets+http: connect Slack + Claude in the org and
  swap `notify-slack` → `slack.sendTextMessage` and `ai-root-cause` → `claude.textPrompt`. More setup, cleaner nodes.
