# Demo video (90 seconds)

Record at 1280×800, browser zoom 110%. Before recording: real accounts connected, ShipMatch running with
the `demo` org, Gmail open in a second window, Slack in a third, the n8n execution list in a fourth.

| Time | On screen | Voice-over |
|---|---|---|
| 0–10 s | The ops Gmail inbox with a few unread emails | "Ops teams spend about three hours a day in their inbox. Watch this." |
| 10–30 s | From a customer account, send: *"rate pls, 4 pallets Lhr→Khi tue"*. Then a second email with an invoice PDF attached | "A messy quote request, and an invoice." |
| 30–50 s | n8n → Executions: WF1, then WF2 running (green nodes). Switch to HubSpot: the new deal "… Lahore→Karachi" in *Info requested*. Switch to ShipMatch: the invoice in the document list | "n8n picks them up, Claude works out what each one needs, the deal lands in HubSpot and the invoice goes straight to ShipMatch." |
| 50–65 s | Slack `#ops-approvals`: the approval card (fields, actions taken, draft asking for the missing weight). Click **Approve and send**. Gmail: the reply in the customer's thread | "Nothing goes out without a person. One click, and the reply lands in the same thread, asking for the missing weight." |
| 65–80 s | Dashboard → Metrics (tiles), then Evaluations (accuracy and cost from the latest report), then the review queue with the injection email open ("Possible prompt injection") | "Every number is measured: accuracy on 62 labelled emails, cost per email, time to reply. And an email that tries to hijack the AI is held for a person." |
| 80–90 s | Architecture diagram | "Works for any shared inbox: sales, support, operations. Set up in two weeks." |

Tips: pre-warm the stack (send one email before recording) so the first execution isn't slow; hide bookmarks
and notifications; use the real evaluation numbers only.
