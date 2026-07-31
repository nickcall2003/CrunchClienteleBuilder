# TrainerCRM

A private kickoff-outreach CRM for a Crunch Cordova personal trainer. FastAPI + Postgres,
deployed on Railway. Both pages (the app and the public sign-up form) are inlined into the
Python, so there are **no folders** — every file sits at the repo root. No member data lives
in this repo; you import your report *after* deploying, into your own private instance.

## Files (all at root, no folders)
- `main.py` – the API + serves both pages
- `pages.py` – the two web pages (app UI + intake form) as inlined HTML
- `models.py`, `database.py` – data layer (Postgres, SQLite fallback)
- `run.py` – start script (reads the PORT Railway gives it)
- `Dockerfile`, `railway.json`, `requirements.txt`, `.gitignore`

## Deploy on Railway (from an iPhone, GitHub web UI)
1. **Repo:** on github.com, create a new repo → **Add file → Upload files** → upload ALL the
   files from this folder (they're all loose — no folders to worry about). Commit.
2. **Railway:** railway.app → New Project → Deploy from GitHub repo → pick the repo. It reads
   the `Dockerfile`.
3. **Database:** in the project, New → Database → PostgreSQL. On your **app service → Variables**,
   add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`.
4. **Accounts:** the app has real logins now. The FIRST person to open it registers and becomes the admin. To let other trainers sign up, set a `REGISTER_CODE` variable and share that code with them; without it, only the first account can be created.
5. **Domain:** app service → Settings → Networking → Generate Domain (enter port **8080**).
   Open the URL, create your account, then Import your report. Add to home screen for the icon.

## Pages
- App: `your-url/`
- Public sign-up: `your-url/intake` — put this in your Instagram bio or a QR for the gym floor.

## Commission levels
PT1 30% · PT2 35% · PT3 40% · Master 45% · Elite 50%. Set yours on the Stats tab.

## Editing the pages later
The app UI and intake form live in `pages.py` as `INDEX_HTML` and `INTAKE_HTML`. Edit that one
file and redeploy — no folders, no separate uploads.

## Run locally
```
pip install -r requirements.txt
python run.py
```
Open http://localhost:8000

## Note on member data
Ships empty on purpose. Only import your Crunch kickoff report into your own private,
account-protected instance, and clear it with your club first — that data is Crunch's.
