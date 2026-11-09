# Customizing Journal Scanner

Every setting below is either a command-line flag or one named line near the top of a file. After changing:
- **a web page** (`scanner_ui/*.html`): reload the page.
- **a `.py` file**: restart the scanner, but only once the queue shows 0, so no reading is cut off. To restart, click the app → Stop it, then open the app again (or Ctrl-C and rerun).
- **`scanner_app/launch.sh`**: rerun `zsh scanner_app/build_app.sh`.

## Where pages are saved

The scanner saves only into one folder (`--out`) and refuses to save if that folder isn't there. That way a journal never lands on an unencrypted disk by accident.

| You want | Do this |
|---|---|
| A vault with another name, e.g. `Journals` | Make the disk image with that name. In `scanner_app/launch.sh`, set `VAULT_NAME="${JS_VAULT_NAME:-Journals}"` and rebuild the app. From Terminal: `python3 journal_scanner.py --out /Volumes/Journals/journals`. |
| A vault elsewhere (external SSD) | Keep the name. The app looks for `<name>.sparsebundle` (or `.sparseimage` / `.dmg`) in `~`, `~/Documents`, and the top two folder levels of every mounted drive. |
| A plain folder, no vault | `python3 journal_scanner.py --out ~/Journals`. For the app, set `VAULT="$HOME/Journals"` in `launch.sh` (replacing the `VAULT=` line). The app then makes `journals/` inside it. FileVault still encrypts it at rest. |
| The default without a flag | `export JOURNAL_SCANNER_OUT=~/Journals` in `~/.zshrc` (Terminal starts only; the app uses `launch.sh`). |

## Models

| Setting | Where | Default |
|---|---|---|
| Handwriting reader | Settings (the button with the dot, in the header) or `--model`; remembered in `~/Library/Application Support/JournalScanner/settings.json` | `qwen3-vl:8b-instruct` (`DEFAULT_MODEL` in `journal_scanner.py`) |
| Merge model (your reading + the draft) | `MERGE_MODEL` in `journal_scanner.py` | `gpt-oss:20b` |
| Photo size sent to the reader | `READ_PX` | `1400` (it read better than 2000 px, in half the time) |
| Speech model | `WHISPER_MODEL` (path to a whisper.cpp `ggml-*.bin`) | `ggml-large-v3-turbo.bin` |
| Speech language | `"-l", "en"` (two places in `journal_scanner.py`) | English. Use `"auto"` or a code like `"de"` |

- **Less memory (16 GB Mac):** try `qwen2.5vl:7b` as the reader and a smaller instruct model for `MERGE_MODEL`.
- **Comparing readers on your own handwriting:** run `python3 transcribe_journal.py <folder of photos> --models qwen3-vl:8b-instruct qwen2.5vl:7b`. It writes one transcript per model, and you score them by eye.
- **Faster readers:** reading is serial (one page at a time). Ollama's own `OLLAMA_NUM_PARALLEL` and `OLLAMA_FLASH_ATTENTION` settings may help, but they apply to every app that uses Ollama on the Mac, so test them.

## What the models are told

| Prompt | File | Used for |
|---|---|---|
| `PROMPT` | `transcribe_journal.py` | Reading a page: keep spelling and line breaks, `[?]` for unreadable words, `~~crossed out~~`. |
| `DESCRIBE_PROMPT` | `journal_scanner.py` | Layout and drawings (never the words). |
| `MERGE_PROMPT` | `journal_scanner.py` | Correcting the draft with what you said. It has examples of speech-recognition slips; add your own. |

To change how pages are written in your transcripts (for example dropping `[?]` marks, or marking dates), edit `PROMPT`. Then move the old transcripts to `_superseded/` and restart, and the scanner rereads them.

## Narrate

- **Spoken commands:** the `SPOKEN` list in `scanner_ui/narrate.html`. Each row maps the words you say to a mark. Add another way of saying something to a row (e.g. `|bullet point` next to `list item`). Keep the speech hint in `narrate_chunk` (in `journal_scanner.py`) matching, so whisper expects those words.
- **Marks in transcripts:** `<u>…</u>` for underlines, `★`, `- ` for list items, `---` for dividers, `[note: …]` for notes (saved to `notes/`, not the transcript). The mark table is in `place()` in `narrate.html`.
- **Wispr Flow by default:** in `narrate.html`, `store.get('flow', false)` → `true`. Each browser remembers its own choice after that.
- **Training counter:** `TRAINING_GOAL` in `journal_scanner.py` (200 pages). It's only a counter; nothing trains on its own.

## Camera and pages

- **New-page sensitivity:** in Settings. Use High if pages are skipped, Low if one page is taken twice.
- **Sideways camera:** the rotate button next to Camera; it's remembered.
- **Without an iPhone:** any camera macOS sees appears under **Camera**.

## Network

| | Flag | Default |
|---|---|---|
| Scanner page (this Mac only) | `--port` | 8770 |
| iPhone and laptop (home network, HTTPS) | `--phone-port` | 8771 |
| Turn the network listener off | `--no-phone` | on |
| Don't open a browser on start | `--no-open` | opens |

If you change `--port`, also change `PORT=` in `launch.sh`. The phone listener uses a certificate authority made just for this Mac (`~/Library/Application Support/JournalScanner/ca.crt`) and a pairing cookie. It never reaches the internet. Ollama is always 127.0.0.1 (`OLLAMA` in `journal_scanner.py`). Point it at another machine only if you accept that page photos travel there.

## Privacy defaults worth keeping

- The log (`~/Library/Logs/JournalScanner.log`) prints names, counts and timings, never page text. Keep it that way if you add `print` lines.
- Mac speech (the default) keeps audio and text local. Wispr Flow mode sends audio to Wispr.
- Nothing is deleted by the app. "Move aside" goes to `_discarded/`, rereads keep old versions in `_superseded/`, and moves are listed in `journals/_moves.log`.
