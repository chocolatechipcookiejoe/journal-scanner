# AGENTS.md: for an AI agent setting this up

Read `README.md` (setup and use), then `CUSTOMIZE.md` (every setting).

## Setting it up for someone

Do the steps in README "Setup" in order. Check each one before moving on:
- `ollama list` shows both models.
- `ls ~/Library/Application\ Support/JournalScanner/whisper/` shows the `.bin`.
- `curl -s http://127.0.0.1:8770/api/status` answers with `"ready": true` and `"ollama": true`.

Ask the person, don't guess:
1. **Where pages go:** an encrypted disk image (recommended), with what name, or a plain folder. Then follow CUSTOMIZE "Where pages are saved".
2. **How much memory the Mac has:** under 32 GB, suggest smaller models.
3. **Which camera:** iPhone mode, Continuity Camera or a webcam.
4. **Wispr Flow for narrating, or local speech only.**

The person does these steps themselves:
- creating the disk image and its password (Disk Utility),
- `xcode-select --install`,
- installing Homebrew if it asks for their password,
- trusting the iPhone certificate on the phone.

## Rules

- **Don't read journal pages, transcripts or recordings** unless the person clears specific pages for you. They're private, and for a cloud model, reading them sends them off the machine. To check quality, use counts (words, `[?]` marks, timings), not text.
- **Keep the log free of page text.** New `print` lines show names, counts and timings only.
- **Standard-library Python only.** No `pip` packages. Models are called over Ollama's local HTTP API (127.0.0.1) and speech through whisper.cpp. Don't point either at a remote host.
- **Restart only when idle:** wait for `"queue": 0` and `"reading": null` from `/api/status`, so nothing in flight is lost. Page (`.html`) changes need only a reload.
- **Nothing is deleted.** Move things to `_discarded/` or `_superseded/` instead.
- **With a second brain (e.g. SOTASECONDBRAIN):** propose notes through its own tools into its inbox. Never write raw journal text into the brain, and never land changes as the owner.
