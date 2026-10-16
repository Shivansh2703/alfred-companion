# Alfred Companion

Alfred is an AI administrator for your Total War: WARHAMMER III campaign. The in-game mod
(installed from Steam Workshop) hands routine settlement building choices and hero/lord skill
points to an LLM, so you can keep playing the parts you care about. Steam Workshop mods cannot
make internet calls or store an API key, so this small companion app runs on your computer,
talks to the model on the mod's behalf, and does nothing else.

- **What it is**: a background app plus the game mod. Vanilla campaigns only today.
- **What it needs from you**: a free Gemini API key (or a local Ollama model — no key, nothing
  leaves your machine).
- **What it sends**: see "What Alfred sends" below — a short state summary per turn, nothing
  else.

## Install on Mac

1. Make sure Total War: WARHAMMER III is installed. Launch it once, then quit back to the
   desktop — this lets your Mac create the folder Alfred needs.
2. Open **Alfred-Mac.dmg**, drag Alfred into the Applications folder shown, then open Alfred
   from Applications.
3. Open Alfred. Your Mac will block it the first time because it isn't from the App Store.
   Go to **System Settings → Privacy & Security**, find the message about Alfred, and click
   **Open Anyway**. Enter your Mac password when asked.
4. Alfred opens a page in your browser (Set up model). Paste your Gemini API key (see below) or
   point Alfred at a local Ollama model, then click **Save**. Alfred sends a short test prompt;
   when that works the page says Alfred is ready. It then starts quietly by itself every time
   you log in — you don't need to open it again unless you want to change settings or stop it.
5. In the game, enable **Alfred** and **Mod Configuration Tool** in the mod list, use the
   Vanilla preset, and turn off any overhaul packs. Play.

## Get a model key

Alfred needs a model to think with. The easiest one is Google's Gemini, which has a free tier.

1. Go to Google AI Studio (aistudio.google.com) and sign in with a Google account.
2. Click "Get API key," create one, and copy it.
3. Paste it into the Alfred setup page in your browser and click **Save**.

**What it costs.** Alfred uses `gemini-3.5-flash-lite`. On Google's pricing page it is listed as
free of charge on the free tier, with lower rate limits. If you move to a paid plan, Google lists
$0.30 per million input tokens and $2.50 per million output tokens (ai.google.dev/gemini-api/docs/pricing,
checked 2026-10-13). I have not measured the cost of a typical session, so check your provider's
pricing before you play long campaigns on a paid key. Running a local Ollama model costs nothing.

The key is saved only on this computer. It is never written into the mod, your savegame, any
log file, or the chat transcript.

## What Alfred sends to Google

Every chat message or campaign sweep sends a short summary to Google's Gemini API: your
faction, treasury, income and upkeep, your settlements and building slots, your characters and
skills, and your wars and armies. Google's free tier may use this content to improve their
products. Ollama, if you use it instead, never leaves this computer.

## If the game says Alfred isn't running

Open Alfred Companion (it should already be running in the background — check for its window or
menu bar icon), then restart the game. If it still doesn't respond, quit and reopen Alfred once.

## After a game update

A game update on the same major version (9.x today) keeps working. Alfred shows no warning.
If Warhammer III moves to a new major version (for example 10), Alfred refuses to start and says
so plainly. Download the newest Alfred Companion from the same place you got this one.

## What this is

- Vanilla only. SFO, Horde, and Ogre camps are unsupported.
- Mac and Windows.
- single-player only. Do not use in multiplayer.

---

## Advanced

- Your key and chat history live in `~/Library/Application Support/Alfred/`, separate from
  Steam Workshop folders and your savegame. Delete that folder to remove them.
- Logs: `~/Library/Application Support/Alfred/daemon/aigen.log`.
- If you have more than one Steam account on this Mac, set the `ALFRED_EXCHANGE` environment
  variable to the right `.../Steam/userdata/<id>/1142710/local/Warhammer3/scripts` folder before
  opening Alfred.
- Alfred runs in the background via a per-user LaunchAgent
  (`~/Library/LaunchAgents/com.alfred.companion.plist`), installed on first open and removed by
  **Stop Alfred**. If you ever need to check it manually from Terminal:
  `launchctl print gui/$(id -u)/com.alfred.companion`.

## Install on Windows

1. Make sure Total War: WARHAMMER III is installed. Launch it once, then quit back to the
   desktop.
2. Download `Alfred-Windows.zip` from the latest release, unzip it anywhere (e.g.
   `Documents\Alfred`), and double-click `Alfred.vbs` to start it. Use `Stop Alfred.vbs` to
   stop it.
3. Windows may show a SmartScreen warning because this isn't signed. Click **More info →
   Run anyway**.
4. Everything else (the Gemini key, what's sent, in-game setup) is the same as the Mac guide
   above.

The Windows package ships an embedded Python — nothing else to install.

## Releasing (for maintainers)

`data/rules.json` (the extracted, modded game rulebook, ~16 MB derived from the game's own
data) is deliberately **not** in this repo — it ships only inside the release zips. The
`v*` tag workflow (`.github/workflows/build.yml`) expects a release asset named
`rules-<game_build_id>.json` (see `vanilla.lock.json`'s `game_build_id`) already attached to
this repo's GitHub releases, and downloads it before building. If that asset is missing, the
build fails with a clear message rather than shipping a Windows/Mac package without the
rulebook. The simplest path for a first release: build both zips locally
(`scripts/build_macos_app.sh`, `tools/build_windows_package.sh` — both need `data/rules.json`
copied in locally first) and upload them by hand; the tag workflow is for repeat releases once
a `rules-*.json` asset already exists.

## Uninstall

- **Mac**: open Alfred, click **Stop Alfred**, delete the app from Applications, unsubscribe
  from the Workshop item. Delete `~/Library/Application Support/Alfred/` to remove your saved
  key and chat history.
- **Windows**: run `Stop Alfred.vbs`, delete the unzipped folder, unsubscribe from the Workshop
  item. Delete `%APPDATA%\Alfred` to remove your saved key and chat history.

## License

MIT — see `LICENSE`.
