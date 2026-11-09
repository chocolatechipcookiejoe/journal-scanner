# Journal Scanner

Turn paper journals into text on your own Mac. Flip through a journal under an iPhone camera. Every clear page is photographed, turned upright, cropped and split into left and right sides. Each side is then read by a local vision model. To correct the reading, read the page aloud (or narrate a whole journal). A local model merges what you said with what it read.

Nothing leaves the Mac unless you choose Wispr Flow for narrating:
- Photos, transcripts and recordings live in an encrypted disk image that you open with a password.
- Models run in [Ollama](https://ollama.com) on 127.0.0.1.
- Speech is turned into text by [whisper.cpp](https://github.com/ggml-org/whisper.cpp) on the same Mac.
- The log has file names and counts, never page text.

It's one standard-library Python file (`journal_scanner.py`), three web pages (`scanner_ui/`) and a small Swift helper that finds pages in photos (`scanner_app/pagefinder.swift`, compiled the first time it's needed). There's nothing to `pip install`.

It works on its own or as an intake for a plain-Markdown second brain such as [SOTASECONDBRAIN](https://github.com/KevinOrosz/SOTASECONDBRAIN). See [Using it with a second brain](#using-it-with-a-second-brain).

> **AI agent setting this up for someone?** Read [AGENTS.md](AGENTS.md) first.

---

## What you need

| | |
|---|---|
| **Mac** | Apple silicon with a recent macOS. It's been used on a 36 GB Mac. The reader and merge models together want about 20 GB of free memory, so 32 GB or more is comfortable. With 16 GB, pick smaller models (see [CUSTOMIZE.md](CUSTOMIZE.md)). |
| **Disk** | About 25 GB for the models, plus roughly 1–2 MB per page photo. |
| **Camera** | An iPhone. Either open the scanner in Safari on the phone (*iPhone mode*, over home Wi-Fi), or use it as the Mac's camera through Continuity Camera. A USB webcam works too. |
| **Tools** | Python 3.9+ and Swift (both come with Xcode Command Line Tools), [Homebrew](https://brew.sh), Ollama and whisper.cpp. |

## Setup (about 30 minutes, mostly downloads)

**1. Command Line Tools and Homebrew.** Skip what you already have:

```bash
xcode-select --install
```

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**2. Ollama and the two models.** Install the Ollama app from [ollama.com](https://ollama.com) and open it. Then pull the models:

```bash
ollama pull qwen3-vl:8b-instruct
```

```bash
ollama pull gpt-oss:20b
```

- `qwen3-vl:8b-instruct` reads the handwriting (about 6 GB). It scored best of the models tried, at about 83% of words right on a messy page.
- `gpt-oss:20b` merges your spoken reading with the draft (about 13 GB).

If a name has changed, pick current ones from [ollama.com/search?c=vision](https://ollama.com/search?c=vision) and see [CUSTOMIZE.md](CUSTOMIZE.md).

**3. whisper.cpp and its speech model** (about 1.6 GB). The scanner looks for the model at exactly this path:

```bash
brew install whisper-cpp
```

```bash
mkdir -p ~/Library/Application\ Support/JournalScanner/whisper && curl -L -o ~/Library/Application\ Support/JournalScanner/whisper/ggml-large-v3-turbo.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin
```

**4. An encrypted vault for the pages.** Journals are private, so the scanner saves only into a folder you pick, by default an encrypted disk image named `JournalVault`:
- Disk Utility → File → New Image → Blank Image.
- Name it `JournalVault`, Size 50 GB (a sparse bundle only uses what it holds), Format APFS, Encryption 256-bit AES, Image Format *sparse bundle*.
- Save it in your home folder.
- It mounts at `/Volumes/JournalVault`, and the scanner makes `journals/` inside it.

You can use a different name, or skip the vault and use a plain folder. See [CUSTOMIZE.md](CUSTOMIZE.md).

**5. Get this folder and start it once from Terminal:**

```bash
cd ~/code/journal-scanner && python3 journal_scanner.py
```

It opens http://127.0.0.1:8770. The first start takes a minute: it compiles the page finder and makes a local certificate for the iPhone. The header's dot turns green when Ollama answers.

**6. Optional: a double-click app.** This builds `~/Applications/Journal Scanner.app`. The app opens Ollama, mounts the vault (macOS asks for its password), starts the scanner in the background and opens it in your browser. Opened again while running, it offers **Open** or **Stop it**. The log goes to `~/Library/Logs/JournalScanner.log`.

```bash
zsh ~/code/journal-scanner/scanner_app/build_app.sh
```

Rerun it if you move this folder.

**7. Check it works.** Make a journal (header → Journal → New journal) and hold one page under the camera. Within a few seconds it's saved, and a transcript appears under it.

---

## Using it

**Scanning.** Click **iPhone** in the header and scan the code with the iPhone's Camera app. The scanner opens in Safari on the phone and uses the phone's own camera. Pages go to the Mac over home Wi-Fi, over HTTPS and paired by that code.

The first time, Safari warns that the connection isn't private. Tap Show Details → visit this website. To stop the warning for good, install the certificate from the phone's Settings button.

Or put the iPhone on a stand near the Mac and pick it under **Camera** (Continuity Camera). Use the rotate button if the page is sideways.

Then flip through the journal. A page is taken when:
- your hands are clear,
- it has held still for about a second,
- it's in focus,
- and its handwriting differs from the last saved page.

Keys: **Space** takes a page by hand, **P** pauses, **Delete** moves the last page aside. If pages are skipped, set New-page sensitivity to High. If the same page is taken twice, set it to Low.

**What gets saved**, in `<vault>/journals/<journal>/`:
- `p0001.jpg`, …: the photos.
- `crops/`: each side, upright.
- `transcripts/p0001.<model>.md`: what the model read.
- `descriptions/`: layout and drawings, not words.
- `readings/`: your recordings and what you said.
- `notes/`: anything you said as a note about the page.

Nothing is deleted: "Move aside" puts a page in `_discarded/`. A closed scanner picks up unread pages the next time it starts.

**Stuck readings.** Now and then (about 3 sides in 100), the model gets stuck repeating a line. The scanner notices while the reading arrives, stops it, and reads that side again with a stronger anti-repeat setting. If the second try also sticks, the transcript ends with a note saying where it stopped.

**Read aloud** (one page). Open a page, tap **🎙 Read aloud**, read it as written, then tap **Stop & save**. Say "note" before anything you want to add about the page, and "back to the page" to carry on. whisper.cpp turns your voice into text, and the merge model corrects the transcript with it. That's the "your reading" tab.

**Narrate** (a whole journal, fastest for correcting). Click **🎙 Narrate**. Each side is its own page, shown upright, and your words appear on the right as you talk.

Keys:
- **Space**: start or pause listening.
- **Enter** or **→**: save the page and turn to the next one.
- **←**: go back.
- **⌘↵**: save while the cursor is in the text box. Click into the text to fix a word.

Spoken commands:
- **Punctuation**: only what you say counts. "period", "comma", "question mark", "colon", "dot dot dot", "quote … quote", "parenthesis … parenthesis", "slash", "dash", "arrow".
- **Formatting**: "new line", "list item", "divider", "… underlined" (after the underlined words), "gold star".
- **Notes**: "note … end note" for a note about the page.

The buttons (and **U**, **D**, **B**, **S**) do the same as the formatting commands. To underline exactly some words, highlight them and press **U**. To change a saved page, go back to it: its reading is in the box, and saving again replaces it.

The counter at the top right counts pages read aloud across all journals. Those readings are a training set if you later want to fine-tune the reader on your handwriting.

**Narrate with Wispr Flow** (optional, online). If you use [Wispr Flow](https://wisprflow.ai), set the **Flow | Mac** switch in the Narrate header to **Flow**. Hold Flow's key and read, and Flow types into the box. Flow's punctuation is kept, and the formatting commands still work. **Enter** is a line break and **⌘↵** saves.

Flow sends your audio to Wispr's servers, and no recording is kept on the Mac in this mode. **Mac** (the default) keeps everything local.

**Organize.** Click **Organize** to split a mixed journal or merge two. Select photos (Shift-click selects a range), then drag them onto another journal. They're renumbered with all their readings, and every move is logged in `journals/_moves.log`.

**From a laptop.** In the iPhone panel, use **Copy laptop link** and open the link in Safari on the laptop. You can narrate from the laptop's microphone while the Mac does the work.

**Continuity Camera keeps dropping?** The no-camera screen shows a live checklist of what macOS sees. The **Reconnect iPhone** button (next to Camera) restarts the Mac's Continuity Camera service. A Mac with no built-in camera (Mac Studio, Mac mini) only offers the iPhone when it's plugged in by cable, or when it's locked, still and standing up. iPhone mode avoids all of this.

---

## Using it with a second brain

The scanner writes plain Markdown, so any notes system can use its transcripts. With SOTASECONDBRAIN, the natural fit is:

1. Scan and correct pages here. The transcripts stay in the vault.
2. Ask your AI agent to read a journal's `transcripts/*.read-aloud.md` (or the model transcripts) and **propose** notes through the brain's own tools (`bin/kb write put` / `link` into `KB/inbox/`).
3. You review and land them yourself (`make land ID=...`). Raw journal text never goes into the brain; only the notes you approve do.

Decide first whether your agent may read journal pages at all. If the agent is a cloud model, reading them sends page text off the Mac. A local model (through Ollama) keeps it on the Mac.

## Files

| | |
|---|---|
| `journal_scanner.py` | The server: camera intake, page finding, reading queue, speech, merge, phone pairing. |
| `transcribe_journal.py` | Compares vision models on a folder of photos (and holds the reading prompt the scanner uses). |
| `scanner_ui/index.html` | Scanner page. `narrate.html`, `organize.html`: the other two. |
| `scanner_app/` | `pagefinder.swift` (page detection with Apple's Vision framework), `launch.sh` and `build_app.sh` for the double-click app, `icon.svg`. |
| `Journal Scanner.command` | Double-click to run in a Terminal window instead of the app. |

Settings, the iPhone certificate and the whisper model live in `~/Library/Application Support/JournalScanner/`.

To change paths, models, ports, prompts or spoken commands, see [CUSTOMIZE.md](CUSTOMIZE.md).
