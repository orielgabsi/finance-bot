# Finance Bot

A Telegram bot and live web workspace for tracking a personal investment portfolio, free cash, personalized risk settings, fundamental stock/fund research, and verified AI recommendations.

- **Telegram bot** — purchases, sales, cash balance, tax scenarios, Excel/photo import, personalized profile, and `/analyze` fundamental research for stocks and funds.
- **Website** (`web/`) — a real-time Firebase dashboard with allocation, holdings, cash management, profile settings, transaction activity, tax scenarios, AI Q&A, and the same fundamental analyzer as Telegram.
- **Verified AI** — every portfolio or fundamental recommendation is sent through a second Groq pass that checks the draft against the original trusted numbers/news and corrects unsupported claims.
- **Weekly email** (`weekly_recommendations.py`, run by GitHub Actions) — combines live holdings, cash, profile, fresh news, and the same two-pass AI process, emailed via Resend.

## 1. Bot setup (Stage 1)

1. Create a virtual environment and install dependencies:
   ```
   pip install -r requirements.txt
   ```
2. Copy `.env.example` to `.env` and fill in real values (at minimum `TELEGRAM_BOT_TOKEN`, `BOT_ACCESS_PASSWORD`, `FIREBASE_SERVICE_ACCOUNT_PATH`).
3. Place your Firebase service account key as `serviceAccountKey.json` in this folder (never commit it — it's in `.gitignore`).
4. Run the bot:
   ```
   python Finance_bot.py
   ```
5. In Telegram: `/start`, enter the password, then use `/buy`, `/sell`, `/cash`, `/analyze`, `/profile`, `/import`, `/portfolio`, `/cake`, `/email`, and `/link` (or the on-screen menu buttons).
   - `/import` with an `.xlsx` file: the file needs a header row with recognizable column names (`Ticker`/`טיקר`, `Quantity`/`כמות`, `Price`/`מחיר` — English or Hebrew). Rows are shown as a preview before anything is saved.
   - `/import` with a photo: a screenshot of a portfolio (any broker/site). Read via a vision AI model (Groq `qwen/qwen3.6-27b`) — less reliable than the Excel path since it depends on image quality, so double-check the preview before confirming.

## 2. Website setup (Stage 2)

The website is a static site (no backend of its own) that reads the same Firestore data the bot writes.

1. **Enable Firebase Auth**: Firebase Console → your project → **Authentication** → Get started → enable the **Email/Password** provider.
2. **Register a Web app**: Firebase Console → **Project settings** → General → "Your apps" → **Add app → Web**. Copy the config object it gives you.
3. Paste those values into `web/firebase-config.js`, replacing every `REPLACE_ME_...` placeholder (`projectId` is already filled in). These values are safe to commit — they're not secret, unlike `serviceAccountKey.json`.
4. **Deploy the security rules**. Portfolio/trade/AI-result writes remain server-only; a proven linked website account may update only its own cash balance and profile. Link codes are atomically consumed once:
   ```
   npm install -g firebase-tools   # one-time
   firebase login
   firebase deploy --only firestore:rules
   ```
5. **Deploy the site** (also via `firebase-tools`):
   ```
   firebase deploy --only hosting
   ```
   This deploys to the `finance-bot-app` Hosting site (set in `firebase.json`), live at **https://finance-bot-app.web.app**. (The project's original default site, `https://my-asistant-298e7.web.app`, still exists but is no longer kept in sync — `firebase deploy --only hosting` only pushes to `finance-bot-app` now.)
6. **Link your account**: sign up on the website, then in Telegram send `/link` to the bot, and enter the one-time code it gives you on the dashboard page. The dashboard listens to Firestore in real time, so bot-side updates appear without refreshing the page.

## 3. Weekly AI recommendations (Stage 3)

1. Get free-tier API keys:
   - **Groq**: [console.groq.com](https://console.groq.com/) → API Keys (used for the AI-written summary; free tier).
   - **Tavily**: [tavily.com](https://tavily.com/) → API Keys (used for web search; free tier, ~1000 searches/month).
   - **Resend**: [resend.com](https://resend.com/) → API Keys. Unlike SendGrid, Resend won't verify a plain address like a Gmail account as a sender — it requires a domain you own. Without one, use their shared sandbox address `onboarding@resend.dev` as `RESEND_FROM_EMAIL` (works immediately, no setup) — **but it can only deliver to the email address of the Resend account itself**, not to arbitrary recipients. Fine for solo use/testing; once you have real users with different emails, you'll need to buy a domain and verify it under Domains in the Resend dashboard to lift that restriction.
2. In Telegram, each user who wants weekly emails sends `/email their@email.com` to the bot.
3. **Local test run** (optional, before relying on the schedule): fill in `GROQ_API_KEY`, `TAVILY_API_KEY`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL` in `.env`, then run:
   ```
   python weekly_recommendations.py
   ```
4. **Set up GitHub Actions** so it runs automatically every Monday (`.github/workflows/weekly_recommendations.yml` already does this — you just need to add the secrets):
   - Push this repo to GitHub (see step 4 below first, for secrets hygiene).
   - Repo → **Settings → Secrets and variables → Actions → New repository secret**, and add each of: `FIREBASE_SERVICE_ACCOUNT_JSON` (paste the *entire contents* of `serviceAccountKey.json` as one secret — not the file, just its text), `GROQ_API_KEY`, `TAVILY_API_KEY`, `RESEND_API_KEY`, `RESEND_FROM_EMAIL`.
   - You can trigger it manually anytime from the repo's **Actions** tab (the workflow has a "Run workflow" button) instead of waiting for Monday, to test it.

## ⚠️ Security — rotate exposed secrets before publishing

Earlier versions of this code had a Telegram bot token and a Groq API key hardcoded in plaintext. Both must be treated as compromised, regardless of any code cleanup:

- **Telegram bot token**: open **@BotFather** in Telegram → `/mybots` → select your bot → **API Token** → **Revoke**, then put the new token in `.env`.
- **Groq API key**: log into the [Groq console](https://console.groq.com/) and delete/rotate the exposed key.

Do this **before** pushing this repo to GitHub. Also double-check `git status` before your first commit to make sure `.env` and `serviceAccountKey.json` are not staged — both are in `.gitignore`, but it's worth a second look before the very first push.

## Project structure

**Bot (Stage 1):**
- `Finance_bot.py` — Telegram bot: commands, menu, conversation flows
- `connect_firebase.py` — Firestore data layer (users, portfolio, transactions, link codes, email)
- `price_service.py` — live price lookups via `yfinance`
- `portfolio_service.py` — portfolio valuation math (cost basis, market value, gain/loss); pure functions, reused by the weekly email job
- `chart_service.py` — generates the portfolio pie chart image
- `portfolio_import.py` — bulk portfolio import: Excel parsing (`pandas`/`openpyxl`, header-matched) and screenshot parsing (Groq vision model)
- `finance_engine.py` — Playwright-based Globes fallback used when Yahoo cannot resolve an Israeli security
- `fundamental_service.py` — deterministic stock/fund metrics, performance/risk calculations, scoring and data-quality assessment via `yfinance`

**Website (Stage 2):**
- `web/index.html`, `web/auth.js` — login/signup page
- `web/dashboard.html`, `web/dashboard.js` — live portfolio workspace, cash/profile controls, tax tools, AI Q&A and fundamental analyzer
- `web/firebase-config.js` — public Firebase web config (fill in after registering a web app)
- `firestore.rules` — security rules: website is read-only, and only for a Telegram account it has proven it controls
- `firebase.json`, `.firebaserc` — Firebase Hosting/deploy config

**Weekly AI email (Stage 3):**
- `ai_recommendation.py` — Tavily web search + Groq LLM write-up
- `email_service.py` — Resend sending
- `weekly_recommendations.py` — orchestrator: loops over users with an email on file, computes valuation, generates the recommendation, sends it
- `.github/workflows/weekly_recommendations.yml` — runs the above every Monday via GitHub Actions
