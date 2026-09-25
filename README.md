# Rally Control

A mobile-friendly tournament board. Anyone with the link can watch live scores, schedule, bracket, and standings. Only the director access code can edit scores, event details, groups, and seeds.

## Run locally

Copy `.env.example` to `.env` in the project root and set `DIRECTOR_ACCESS_CODE` to a private value. On first visit, viewers and directors choose a tournament from the directory. After director sign-in, enter the event name, venue, and number of courts. Add seeded players using `1 | Player Name`, then generate group-stage matches. Qualification ranks each group by wins first, then total score difference, then seed. When all group matches are final, qualify the top two from every group to create the knockout tree.

```powershell
.\myapp\Scripts\python.exe -m pip install -r requirements.txt
.\myapp\Scripts\python.exe -m uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`. Viewers can watch without signing in. Select **Director sign in** to edit. Local data is stored in `tournament.db`.

## Share with everyone

For people on the same Wi-Fi, run:

```powershell
.\myapp\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Find your computer's local IPv4 address with `ipconfig`, then viewers open `http://YOUR_LOCAL_IP:8000`.

For access from anywhere, deploy the app to Render, Railway, Fly.io, or another host. Add `DIRECTOR_ACCESS_CODE`, `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `SUPABASE_SERVICE_ROLE_KEY` as private environment variables on that host and share its HTTPS URL. Viewers do not need a code, and your computer does not need to stay on.

### Free Render deployment

1. Push this project to a GitHub repository.
2. Create a **Web Service** at Render and connect the repository.
3. Render can use `render.yaml` automatically. If entering settings manually, use:
	- Build command: `pip install -r requirements.txt`
	- Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
	- Plan: Free
4. Add these environment variables in Render's Environment settings:
	- `DIRECTOR_ACCESS_CODE`: your private director code
	- `SUPABASE_URL`: your Supabase project URL
	- `SUPABASE_ANON_KEY`: your Supabase anon key
	- `SUPABASE_SERVICE_ROLE_KEY`: your Supabase service-role key, kept server-side only
5. Deploy and share the generated `https://...onrender.com` URL. Viewers open it directly; the director uses the same URL and the private code.

The free service may sleep when unused, but your tournament data remains in Supabase and your computer does not need to run.

## Supabase

Run `supabase/schema.sql` in Supabase SQL Editor. The SQL creates tables, read-only viewer policies, director write policies, and Realtime publication. With `SUPABASE_SERVICE_ROLE_KEY` configured, the app uses Supabase for tournaments, groups, players, matches, scores, resets, and deletions. Without it, local development falls back to SQLite. Never put the service-role key in GitHub, browser code, or a phone.
