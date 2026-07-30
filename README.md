# TrainerCRM

A private kickoff-outreach CRM for a Crunch Cordova personal trainer. FastAPI backend +
single-page frontend (served by the same app) + Postgres on Railway. No member data lives
in this repo — you import your report *after* deploying, into your own private instance.

## What's inside
- **Pipeline** – work your kickoff list, sorted by soonest to expire
- **Stats** – funnel, 7-day activity, sales + your commission (by PT level)
- **Grow** – referral leaderboard, Instagram/DM quick-add, leads-by-source
- **Import** – paste your report; it maps name / phone / email / expiration / calendar-pending
- **Text queue** – one-tap personal texts, prefilled with each first name
- **Guides** – pricing cheat sheet (your real numbers), close sequence, content plan
- **/intake** – a public sign-up page people fill out; submissions drop straight into Pipeline

## Commission levels (built in)
PT1 30% · PT2 35% · PT3 40% · Master PT 45% · Elite PT 50%.
Set yours in the app: Stats tab → tap the level next to "Your commission."

## Deploy on Railway (from an iPhone, GitHub web UI)
1. **Make the repo:** on github.com, create a new repo, then "Add file → Upload files" and
   upload this whole folder (keep the `backend/` and `frontend/` folders intact). Commit.
2. **Railway:** at railway.app → New Project → **Deploy from GitHub repo** → pick this repo.
   Railway reads the `Dockerfile` automatically.
3. **Add the database:** in the project, **New → Database → PostgreSQL**. Railway sets
   `DATABASE_URL` for you — the app picks it up automatically (falls back to SQLite locally).
4. **Lock it down (do this):** Project → Variables → add `APP_PASSWORD` = a password only you
   know. The app will ask for it once and remember it on your phone. The `/intake` sign-up page
   stays public so prospects can submit.
5. **Open your app:** Railway gives you a URL. Open it, enter your password, then go to the
   **Import** tab and paste your kickoff report. Add the URL to your home screen for an app icon.

## The sign-up form
Your public form is at `your-url/intake`. Put that link in your Instagram bio or make a QR
for the gym floor. Every submission appears in your Pipeline tagged "Web form," referral and all.

## Environment variables
| Variable | Purpose | Set by |
| --- | --- | --- |
| `DATABASE_URL` | Postgres connection | Railway (automatic) |
| `APP_PASSWORD` | Locks the app (recommended) | You |
| `PORT` | Web port | Railway (automatic) |

## Run locally
```
pip install -r requirements.txt
uvicorn backend.main:app --reload
```
Open http://localhost:8000

## A note on member data
This repo ships empty on purpose. Only import your Crunch kickoff report into your own private,
password-protected instance, and clear it with your club first — that data is Crunch's. Your own
leads (web-form, referrals, DMs) are yours.
