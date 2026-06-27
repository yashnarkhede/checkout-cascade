# Phase 3 — Issue → AI Fix → PR (software-factory loop)

Closes the loop: a `/fix` comment on a GitHub issue makes **DeepSeek read the code and open a PR**.
SuperPlane orchestrates; the actual coding runs in **GitHub Actions** (`.github/workflows/ai-fix.yml`)
using `aider` + your Azure DeepSeek key — free, no SuperPlane runner needed (runners are plan-gated here).

```
/fix comment ─▶ SuperPlane github.onIssueComment (^/fix)
                 ─▶ comment "on it"  ─▶ github.runWorkflow(ai-fix.yml, issue_number)
                                            └▶ GitHub Action: aider+DeepSeek edits repo
                                               ─▶ branch ─▶ PR ─▶ comments PR link on the issue
Bridge: Phase-2 incident ─▶ AI Root Cause ─▶ github.createIssue (diagnosis)  ─▶ a human comments /fix
```

## Engine (already committed): `.github/workflows/ai-fix.yml`
`workflow_dispatch(issue_number)` → reads the issue → `aider --model openai/DeepSeek-V4-Pro`
(with `OPENAI_API_BASE=…/openai/v1`, `OPENAI_API_KEY=secrets.AZURE_DEEPSEEK_KEY`) → pushes a branch
→ `gh pr create` → comments the PR link. Guards: if no changes, comments "no changes"; runs are
isolated per `github.run_id` branch.

## Prerequisites (you, once)
1. **Push the repo to GitHub** (Action must live on `main` for `workflow_dispatch`):
   `gh repo create checkout-cascade --private --source=. --push` (or your existing remote).
2. **Repo secret:** Settings → Secrets and variables → Actions → `AZURE_DEEPSEEK_KEY` = your Foundry key.
3. **Allow Actions to open PRs:** Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests".
4. **Connect the GitHub integration in SuperPlane** → Integrations → Add → GitHub → grant the `checkout-cascade` repo. (CLI can't create integrations.)
5. Read the IDs/names (reads work):
   - `superplane integrations list` → GitHub integration id (+ the OpenAI integration id from Phase 2)
   - `superplane integrations list-resources --type repository` → the repo **resource name**

## Build (UI — CLI writes are 500 on this org)
Open `golden-mission` → Canvas → paste `phase2/canvas.yaml` (one canvas, two graphs), replacing:
- `REPLACE_OPENAI_INTEGRATION_ID` (Phase-2 AI node)
- `REPLACE_GITHUB_INTEGRATION_ID` (all github nodes)
- `REPLACE_GITHUB_REPO_RESOURCE` (repo resource name, NOT owner/repo)
Then **Publish**. (Also create the Phase-2 OpenAI integration + the `openai`/Azure secret if not done — see BUILD-IN-UI.md.)

## Verify (end-to-end)
1. **Fixer:** open an issue in checkout-cascade, comment `/fix add a GET /version endpoint to checkout-service`.
   Expect: SuperPlane run fires (`superplane runs describe`), the Action runs (repo → Actions), a **PR opens**,
   and a **PR-link comment** lands on the issue.
2. **Bridge:** inject `db_slowdown/6` on payment (`curl -X POST https://checkout-cascade.onrender.com/admin/chaos/db_slowdown/6 -H "X-Chaos-Key: <KEY>"`).
   Within ~1 min Phase-2 files a GitHub **issue** with the DeepSeek diagnosis. Comment `/fix …` on it → PR.

## Notes / gotchas
- **No infinite loops:** `onIssueComment` only fires on `^/fix`; the bot's "on it" and PR-link comments don't match.
- **Synthetic incident:** the Phase-2 incident is an injected flag (no real bug), so phrase `/fix` as a real
  resilience change (e.g. "shorten checkout's payment timeout + return a friendly error") so aider produces a meaningful PR.
- **aider + Azure DeepSeek:** routed via the OpenAI-compatible `/openai/v1` base. If litellm rejects the model id,
  fall back to a small Python step in the Action that asks DeepSeek for a unified diff and `git apply`s it.
- **Optional full autonomy:** add a `github.createIssueComment` node after `File Incident Issue` posting `"/fix …"`
  on `{{ $['File Incident Issue'].data.number }}` to auto-trigger the fixer (default is human-in-the-loop).
- **Rotate** the Azure Foundry key + SuperPlane service-account token (both were pasted in chat).
