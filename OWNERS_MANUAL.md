# Owner's manual

## Where everything lives (`data/bot.db`)

Everything is in one SQLite file. Open it with any SQLite viewer (for example the free *DB Browser for SQLite*), or use the `/` commands below.

| Table | What's in it |
|---|---|
| `events` | Logs by `kind`: `leads` (`source: form` from the menu, `source: ai` spotted by Claude), `handover`, `waitlist`, `bookings`, `payments_to_verify`, `refunds_needed`, `human_replies` |
| `bookings` / `holds` | Confirmed bookings and 10-minute slot holds |
| `messages` | Every message in and out, per customer |
| `contacts` | Every customer: name, first seen, last message, paused or not |
| `chats` | The AI's memory per customer |
| `reminders_sent` | Which reminders went out, so none repeat |

## Taking over a chat

- A chat is handed to you when the customer taps **Talk to a human** or types "human", or when Claude decides to hand over (complaint, refund, legal, a big custom deal). The bot logs it in `handover.jsonl`, alerts `OWNER_PHONE` and then **stays silent** for that customer for 12 hours (`HANDOVER_PAUSE_HOURS`).
- Your alert says, for example, `Handover #3 … Reply with: /r 3 your message`. From your phone, text the bot:
  - `/r 3 Hi Rahul, this is Sahil - happy to help!` to reply as yourself (within 24 hours of their last message)
  - `/resume 3` when you're done, to hand them back to the AI
- `/handovers` shows everyone waiting; `/help` lists every command.
- From a computer, the same works with `python bot.py send 919812345678 "…"` and `python bot.py resume 919812345678`.
- The customer can also type "menu" or tap any menu button to get the bot back.

Alerts reach your WhatsApp only if you have messaged the bot's number yourself in the last 24 hours. That's WhatsApp's rule. Send it a "hi" each morning.

## Payments

- **demo**: anyone can "pay". The QR and ticket say DEMO. Never use this with real customers.
- **manual**: check your UPI app for the amount and time, then run `python bot.py confirm <hold_id>`. The hold ID is in the bot log, in `payments_to_verify.jsonl` and in your alert. The customer gets the ticket automatically.
- **razorpay**: nothing to do. Razorpay's signed webhook confirms the booking and sends the ticket. Refunds are handled by you in the Razorpay dashboard.

Never confirm a booking just because a customer says "I paid".

## Daily checklist (5 minutes)

1. Send "hi" to the bot from your phone. This keeps owner alerts working.
2. Text `/summary` to the bot, or set `OWNER_SUMMARY_TIME=20:00` to get it automatically every evening.
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
- [ ] Templates `booking_reminder`, `daily_summary` and `follow_up` approved in Meta (see README → Templates to submit)
- [ ] `OWNER_PHONE` set and `/help` answered from your phone
- [ ] `knowledge.md` and `business.toml` reviewed: real prices, policies, hours, UPI ID, no TODOs left
- [ ] Real `assets/guide.pdf` (and logo/photo) in place
- [ ] `PAYMENT_MODE` is `manual` or `razorpay`, never `demo`. Razorpay webhook tested in test mode
- [ ] `data/` on a persistent disk and backed up daily
- [ ] Anthropic spend limit and alerts set; model chosen for cost
- [ ] Handover tested end to end (tap → alert → `send` → `resume`)
- [ ] 10 end-to-end test conversations in Hinglish and English
