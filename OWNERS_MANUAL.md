# Owner's manual

## Where everything lives (`data/`)

| File | What's in it |
|---|---|
| `leads.jsonl` | Every lead. `source: form` comes from the menu brief; `source: ai` was spotted by Claude in a chat (need, budget, timeline) |
| `bookings.json` | Live holds and confirmed bookings (ticket, time, payment) |
| `bookings.jsonl` | Log of every confirmed booking |
| `handover.jsonl` | Chats waiting for you, with the reason |
| `waitlist.jsonl` | Waitlist emails |
| `payments_to_verify.jsonl` | In `manual` payment mode: customers who tapped "I've paid" |
| `refunds_needed.jsonl` | Paid, but the slot was gone by the time the payment landed. Rebook or refund |
| `contacts.json` | Every customer: name, first seen, last message, paused or not |
| `chats.json` | The AI's memory per customer |

Each `.jsonl` file has one record per line, so you can open it in any text editor or import it into Google Sheets.

## Taking over a chat

- A chat is handed to you when the customer taps **Talk to a human** or types "human", or when Claude decides to hand over (complaint, refund, legal, a big custom deal). The bot logs it in `handover.jsonl`, alerts `OWNER_PHONE` and then **stays silent** for that customer for 12 hours (`HANDOVER_PAUSE_HOURS`).
- To reply as yourself:

  ```bash
  python bot.py send 919812345678 "Hi Rahul, this is Sahil - happy to help!"
  ```

  This only works within 24 hours of the customer's last message.
- When you're done, hand the chat back to the AI:

  ```bash
  python bot.py resume 919812345678
  ```

  The customer can also type "menu" or tap any menu button.

Alerts reach your WhatsApp only if you have messaged the bot's number yourself in the last 24 hours. That's WhatsApp's rule. Send it a "hi" each morning.

## Payments

- **demo**: anyone can "pay". The QR and ticket say DEMO. Never use this with real customers.
- **manual**: check your UPI app for the amount and time, then run `python bot.py confirm <hold_id>`. The hold ID is in the bot log, in `payments_to_verify.jsonl` and in your alert. The customer gets the ticket automatically.
- **razorpay**: nothing to do. Razorpay's signed webhook confirms the booking and sends the ticket. Refunds are handled by you in the Razorpay dashboard.

Never confirm a booking just because a customer says "I paid".

## Daily checklist (5 minutes)

1. Send "hi" to the bot from your phone. This keeps owner alerts working.
2. Run `python bot.py summary` (or `--send` to get it on WhatsApp) to see chats, leads, bookings, revenue and handovers.
3. Reply to every handover, then `resume` the chat.
4. Follow up on new leads.
5. Check the Anthropic Console spend and the host's logs for `[error]` / `[whatsapp]` lines.
6. Back up `data/`. On Railway, take a volume snapshot or download the folder.

## Go-live checklist

- [ ] Permanent System User token in `WHATSAPP_TOKEN` (temporary token deleted)
- [ ] `WHATSAPP_APP_SECRET` set (unsigned requests are rejected)
- [ ] Hosted with a fixed HTTPS URL, Callback URL updated in Meta, `messages` subscribed
- [ ] `python bot.py subscribe` shows your app listed
- [ ] Real business number, display name approved, business verified
- [ ] Business profile says "Replies may be AI-assisted"
- [ ] Templates approved for anything sent outside 24 hours (reminders, follow-ups)
- [ ] `knowledge.md` and `business.toml` reviewed: real prices, policies, hours, UPI ID, no TODOs left
- [ ] Real `assets/guide.pdf` (and logo/photo) in place
- [ ] `PAYMENT_MODE` is `manual` or `razorpay`, never `demo`. Razorpay webhook tested in test mode
- [ ] `data/` on a persistent disk and backed up daily
- [ ] Anthropic spend limit and alerts set; model chosen for cost
- [ ] Handover tested end to end (tap → alert → `send` → `resume`)
- [ ] 10 end-to-end test conversations in Hinglish and English
