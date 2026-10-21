# WhatsApp Agent

A WhatsApp assistant for your business on Meta's **official WhatsApp Business Platform (Cloud API)**, with **Claude** as the brain.

- Tap menus, booking, payments, files, waitlist and human handover are **scripted**: instant, exact, no AI cost.
- Typed messages go to Claude. It answers from `knowledge.md` in English or Hinglish, books calls from phrases like "kal shaam call?", captures leads and hands over to you when needed.

## What to fill in (the only edits you need)

| File | What goes in it |
|---|---|
| `.env` | Secrets: Meta token and IDs, Claude API key, payment keys. **Never paste these into a chat.** |
| `business.toml` | Brand name, colours, menu, services and prices, call types, working hours, files, UPI ID |
| `knowledge.md` | What the AI knows: about you, tone, prices, FAQs, what to decline, when to hand over |
| `assets/` | `logo.png`, `photo.jpg` (optional) and your real `guide.pdf` (the current one is a placeholder) |

Restart the bot after editing any of them.

## Files

```
bot.py        webhook server + owner commands (python bot.py help)
router.py     decides: scripted flow, Claude, or human
flows.py      menus, brief form, booking + payment, files, waitlist, handover
brain.py      Claude: persona, tools (slots / file / handover), memory, lead capture
slots.py      free slots, 10-minute holds, bookings
payments.py   demo / manual UPI / Razorpay (webhook-confirmed)
cards.py      branded images: banner, rate card, QR card, ticket
whatsapp.py   Cloud API sender (limits, retries, media upload) + fake sender for tests
store.py      JSON storage in data/
tests/        offline tests (free) + persona test (real Claude)
```

## Phase 0: accounts (about 15 minutes)

**Meta (WhatsApp)**
1. developers.facebook.com → My Apps → **Create app** → use case **"Connect with customers through WhatsApp"** (only that one) → choose or create a business portfolio → Create.
2. Use cases → Customize → **Step 1. Try it out**. Copy the **Phone Number ID** and **WhatsApp Business Account ID** into `.env` (`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_WABA_ID`).
3. Click **Generate token** and paste it into `WHATSAPP_TOKEN`. This token lasts about 24 hours; see the production section for a permanent one.
4. Under **Recipient**, add your own WhatsApp number and enter the code you receive. Click **Send message**, then reply "hi" on your phone.
5. App settings → Basic → **App secret** → `WHATSAPP_APP_SECRET`.
6. Invent any password for `WHATSAPP_VERIFY_TOKEN`. It is not a Meta key.
7. Ignore for now: "Become a Tech Provider", "Publish", "Permissions and features", and Meta's own AI model pages.

**Anthropic (Claude API)**
1. console.anthropic.com → Billing → buy a small credit (USD 5 is plenty to build and test). **Turn auto-reload OFF** and set a spend alert.
2. API keys → **Create key** → paste it into `ANTHROPIC_API_KEY`. The key is shown only once.

Then check what's set (values are never printed, only their length):

```bash
.venv/bin/python bot.py check
```

## Phase 1: run and test locally

```bash
git clone https://github.com/sahilxnahar/whatsapp-agent.git
cd whatsapp-agent
cp .env.example .env
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

The 21 offline tests are free. They cover the menu, brief form, free and paid booking, hold expiry, waitlist, handover, file delivery, the Claude tool loop, lead capture, error self-healing and the webhook signature.

After adding your Claude key:

```bash
.venv/bin/python tests/persona_test.py
.venv/bin/python bot.py chat
```

`persona_test.py` costs a few cents. It checks the FAQ answer, honesty about being an AI, off-topic and restricted declines, Hinglish booking, lead capture and handover. `bot.py chat` lets you talk to the bot in the terminal; type `/1`, `/2` to tap options.

## Phase 2: connect to WhatsApp (laptop + tunnel)

1. Start the bot:

   ```bash
   .venv/bin/python bot.py serve
   ```

2. In a second terminal, start the tunnel. Use `--protocol quic`, because antivirus HTTPS scanning breaks the default mode:

   ```bash
   cloudflared tunnel --protocol quic --url http://localhost:8000
   ```

   Wait about 30 seconds. Opening the `https://….trycloudflare.com` address should show "… WhatsApp agent is running".
3. Meta → WhatsApp → **Configuration** (Step 2, Production setup) → **Configure webhooks**:
   - Callback URL: `<tunnel address>/webhook`
   - Verify token: your `WHATSAPP_VERIFY_TOKEN`
   - Click **Verify and save**, then under Webhook fields make sure **messages** is **Subscribed**.
4. Link the app to your WhatsApp account. Without this, messages never arrive (grey ticks):

   ```bash
   .venv/bin/python bot.py subscribe
   ```

5. Send "hi" to the test number. The log shows `[in ]`, `[out]`, `[tap]`, `[claude]`, `[tool]`, `[lead]`, `[booking]` lines.

Quick-tunnel addresses change on every restart, so re-paste the Callback URL each time. Hosting fixes this.

## Phase 3: production (real customers)

1. **Permanent token**: business.facebook.com → Settings → Users → **System users** → Add (Admin) → assign your app (full control) and WhatsApp account → **Generate token** with `whatsapp_business_messaging` and `whatsapp_business_management`, expiry **Never** → put it in `WHATSAPP_TOKEN`.
2. **Real number**: Meta → Step 2 → add your number. It can't be active on the normal WhatsApp app at the same time unless you use Coexistence through a provider. Then get the display name approved, add a payment method, complete **Business verification** (Step 3) and update `WHATSAPP_PHONE_NUMBER_ID`. In the business profile, add "Replies may be AI-assisted".
3. **Hosting (Railway, simplest)**: push this folder to a **private** GitHub repo. In Railway, create a service from the repo, add the `.env` values as **Variables**, attach a **Volume** mounted at `/data` and set `DATA_DIR=/data`. Set the health check path to `/` and keep the service always on. The `Procfile` runs:

   ```bash
   gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT "bot:create_app()"
   ```

   Keep **1 worker**: sessions, dedupe and the hold sweeper live in memory. Then set the Callback URL in Meta once, to `https://<your-app>.up.railway.app/webhook`.
4. **Payments**: set `PAYMENT_MODE` to one of these:
   - `demo`: a fake QR clearly labelled DEMO. Testing only.
   - `manual`: a real UPI QR to `upi_id`. You check your UPI app, then run `python bot.py confirm <hold_id>`.
   - `razorpay`: a payment link per booking, confirmed **only** by Razorpay's signed webhook. In the Razorpay Dashboard → Webhooks, add `https://<host>/razorpay/webhook` with the event `payment_link.paid` and a secret, then put that secret in `RAZORPAY_WEBHOOK_SECRET` and your API keys in `RAZORPAY_KEY_ID`/`RAZORPAY_KEY_SECRET`. Start in test mode.
5. **Templates**: messaging someone 24+ hours after their last message needs an approved template (booking reminder, payment received, follow-up). Create them in WhatsApp Manager and send only to people who opted in.
6. Run through the go-live checklist in `OWNERS_MANUAL.md`.

## Change the AI model

Edit `CLAUDE_MODEL` in `.env` (or the host variables) and restart. Then run `python tests/persona_test.py` to compare tone.

| Model | About per 100 typed messages | Use for |
|---|---|---|
| `claude-opus-5-5` | $1.50–2.15 (Rs 130–190) | best tone, demos |
| `claude-sonnet-5-5` | $0.75–1.07 (Rs 65–95) | balanced |
| `claude-haiku-5-5` | $0.04–0.06 (Rs 3.5–5) | everyday volume |

- Menus, buttons, slots, QR codes, tickets and PDFs cost nothing.
- Booking messages cost about twice a plain question, because Claude calls the slot tool and then replies.
- The knowledge base is prompt-cached, so the first message after about 5 idle minutes costs a little more.
- WhatsApp replies within 24 hours are free; utility templates cost a few paise and marketing messages about Rs 0.8–0.9 each in India.
- Opus and Sonnet automatically retry on Anthropic's recommended fallback model if a safety filter wrongly blocks a harmless message. Haiku has no fallback.

## Update the knowledge, rotate tokens

- **Knowledge**: edit `knowledge.md` or `business.toml` and restart. Saved chats carry over. The bot notices the change and drops old reasoning blocks automatically, which prevents "Invalid signature in thinking block" errors.
- **Tokens**: if a token or key ever appeared in a screenshot or chat, regenerate it in Meta or the Anthropic Console, update `.env` or the host variables, and restart.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Grey ticks, nothing in the log | Run `python bot.py subscribe`, and check that the `messages` webhook field is Subscribed |
| `[whatsapp] 401 … code 190` | Token expired. Generate a new token or use a permanent System User token, then restart |
| Meta "Verify and save" fails | Bot or tunnel not running, an old URL, or the verify token doesn't match `.env` exactly |
| Tunnel URL won't resolve (Cloudflare 1016/1033) | Wait 30 seconds, or restart the tunnel and re-paste the new URL |
| cloudflared `x509` error | Use `--protocol quic` |
| "technical issue" replies | Read the `[claude]` log line: key, credit or model name. Thinking-signature errors heal themselves; `python bot.py reset <phone>` clears one chat's memory |
| Only 5 numbers can chat | That's the test number's limit. Move to your real number |
| Can't message a customer first | You're outside the 24-hour window. Use an approved template |
| Works, then stops | The laptop slept or the tunnel URL changed. Host it |
| Interactive message rejected (400) | Shouldn't happen: the sender enforces WhatsApp's limits. Check `[whatsapp]` for details |

## Contributing

Issues and pull requests are welcome. Please run the offline tests before opening a PR:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE) © 2026 Sahil Nahar.

Not affiliated with or endorsed by Meta, WhatsApp or Anthropic. You are responsible for following the [WhatsApp Business Messaging Policy](https://business.whatsapp.com/policy) and [Anthropic's Usage Policy](https://www.anthropic.com/legal/aup) when you run this bot.
