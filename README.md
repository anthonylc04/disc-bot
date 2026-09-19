# disc-bot

A personal Discord planner bot. Tell it what you're doing today or tomorrow; it DMs you when
it's time, with an optional heads-up a few minutes before.

## Commands

| Command | What it does |
|---|---|
| `/today time task [notif] [desc]` | Reminder for today. Rejected if the time has already passed. |
| `/tmrw time task [notif] [desc]` | Reminder for tomorrow. |
| `/list [day]` | Upcoming reminders: all, today, or tomorrow. |
| `/cancel reminder` | Cancel one. Start typing to pick from a list. |
| `/agenda` | Post today's agenda checklist now (replaces the day's existing one). |

- **time**: `3pm`, `3:30pm`, `9am`, `15:00`, `1530`, `noon`, `midnight`. A bare `3` is rejected as
  ambiguous (AM or PM?). A time with a colon but no am/pm is 24-hour, so `9:00` means 9 AM.
- **notif**: minutes of warning before the event (default `0` = no warning). With `notif:10` you get
  a warning 10 minutes before **and** a message at the event time.
- The bot always replies with exactly when it scheduled the reminder. Check that line.

## The daily agenda

At `AGENDA_HOUR` (default 10 AM) the bot posts one embed in `AGENDA_CHANNEL_ID` listing the day's
plan, and edits that same message as the day goes on:

- ⬜ still to come, 🔔 its time has passed, ✅ checked off, ⬛ missed while the bot was offline.
- A **Mark done…** dropdown checks tasks off (several at once). A checked task stops firing.
- A **Refresh** button re-renders it from the database.
- Every reminder DM also carries **Done** and **Snooze** buttons. Snooze pushes it out by
  `SNOOZE_MINUTES` (default 10) with no second warning.

The buttons keep working after a restart: every component has a fixed id that is registered at
startup, and the state lives in SQLite, not in memory.

**Channel permissions:** the bot needs **View Channel**, **Send Messages** and **Embed Links**
in the agenda channel. Without Embed Links the agenda can't be posted at all.

### How "today" and "tomorrow" work (day cutoff)

The day rolls over at `DAY_CUTOFF_HOUR` (default **4 AM**), not midnight:

- At 1 AM Wednesday you're still in Tuesday, so `/tmrw 3pm` means **Wednesday** 3 PM.
- Times before the cutoff belong to the end of a day. On Tuesday, `/today 1am` means tonight
  (Wednesday 1 AM).

### If the bot was offline

Reminders are stored in SQLite, so restarts lose nothing. When the bot comes back:

- An event it missed is sent late, labeled as late.
- A warning for an event that already started is skipped; the event message covers it.
- Anything more than `MISSED_MAX_HOURS` (default 12) overdue is marked missed without a message.

## Setup (Windows, PowerShell)

Requires Python 3.11+.

```powershell
# from the repo root
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
python -m pip install --upgrade pip
pip install -e ".[dev]"

Copy-Item .env.example .env
notepad .env                           # fill in DISCORD_TOKEN, GUILD_ID, OWNER_ID
```

### Discord application

1. Go to https://discord.com/developers/applications and click **New Application**.
2. **Bot** tab: click **Reset Token** and put the token in `.env`. Leave the privileged intents off.
3. **OAuth2 → URL Generator**: select the scopes `bot` and `applications.commands` and the permission
   **Send Messages**. Open the generated URL and invite the bot to your server.
4. In Discord, turn on Developer Mode (Settings → Advanced). Right-click your server → **Copy Server ID**
   (`GUILD_ID`), right-click yourself → **Copy User ID** (`OWNER_ID`), and right-click the channel you
   want the agenda in → **Copy Channel ID** (`AGENDA_CHANNEL_ID`).
5. Right-click your server → **Privacy Settings** and make sure **Direct Messages** is on.

## Run

```powershell
python -m bot
```

You should see `Synced 4 command(s)` and `Logged in as ...`. The commands are registered to your
server only, so they show up right away.

## Tests

```powershell
pytest
```

## Layout

```
bot/
  __main__.py    entry point (python -m bot)
  client.py      bot setup, command sync, persistent component registration
  planner.py     slash commands + reminder loop + agenda posting
  agenda.py      the agenda embed (icons, lines, counts)
  views.py       dropdown, refresh button, Done/Snooze buttons
  notifier.py    what's due and what the message says
  scheduling.py  today/tomorrow + time -> UTC instant (day cutoff, DST)
  timeparse.py   "3:30pm" -> time
  db.py          SQLite storage (UTC epoch seconds)
  config.py      .env loading and validation
tests/
```
