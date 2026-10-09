# Template: AI inbox triage with Slack approval (Gmail + Claude)

A simplified, self-contained version of AI Ops Inbox for n8n's template library: no Django, no database,
11 nodes. File: `gmail-claude-slack-approval.json`.

```
New email (Gmail) → Settings → Claude: triage + draft → Parse result → Reply needed?
   ├ yes → Ask for approval in Slack (Approve and send / Reject, 24 h) → Approved? → Reply in the same thread
   └ no (other, or possible prompt injection) → Tell Slack (no reply)
```

**What it does:** Claude (structured output) classifies each new email, writes a one-line summary and drafts a
reply. The draft goes to Slack with Approve / Reject buttons (n8n's *send and wait*). Only an approved reply is
sent, to the original sender, in the same Gmail thread. Newsletters and anything that tries to instruct the AI
are reported to Slack and never answered.

**Setup**
1. Import the JSON (Workflows → Import from file).
2. Create three credentials: Gmail OAuth2, Anthropic API, Slack (bot token, bot invited to the channel).
3. Edit the **Settings** node: Slack channel, signature, model (default `claude-sonnet-5-5`).
4. In Gmail, create the label `ai-handled`; add it to emails you want skipped.
5. Activate.

**What the full project adds** (this repository): HubSpot and ShipMatch integration, an evidence check on every
extracted value, output checks on drafts (no new links, addresses or amounts), exactly-once actions under retries,
an error workflow, a dashboard with review queue and metrics, and a 62-email evaluation.

**Before publishing to n8n's library:** run it once end to end with real credentials, then remove the
credential IDs that n8n adds on export (the shipped file has none).
