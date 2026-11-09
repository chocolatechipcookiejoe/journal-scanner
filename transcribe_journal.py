#!/usr/bin/env python3
"""Transcribe photos of handwritten journal pages with local vision models.

Everything stays on this Mac: images go only to Ollama on 127.0.0.1, and
transcripts are written next to the photos. The script never prints page
text, only file names, word counts and timings.

    python3 transcribe_journal.py /Volumes/JournalVault/journals/pilot
    python3 transcribe_journal.py PAGES --models qwen2.5vl:7b gemma3:27b
"""
import argparse
import base64
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

OLLAMA = "http://127.0.0.1:11434/api/generate"  # local only, never a remote host
MODELS = ["qwen2.5vl:7b", "qwen2.5vl:32b", "gemma3:27b"]
EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff"}
PROMPT = """Transcribe the handwriting on this journal page exactly as written.
- Keep the writer's words, spelling and line breaks. Do not correct, summarize or explain.
- If a word is unreadable, write [?]. If you are unsure of a word, write your best guess followed by [?].
- Mark crossed-out text as ~~text~~.
- Output only the transcription."""


def prepare(path):
    """Return the page as a JPEG, at most 2000px on its long side (sips ships with macOS)."""
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "page.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "2000", str(path), "--out", str(out)],
                       check=True, capture_output=True)
        return out.read_bytes()


def transcribe(model, image):
    body = json.dumps({"model": model, "prompt": PROMPT, "stream": False,
                       "images": [base64.b64encode(image).decode()],
                       "options": {"temperature": 0}}).encode()
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["response"].strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pages", type=pathlib.Path, help="folder of page photos")
    ap.add_argument("--models", nargs="+", default=MODELS, help="Ollama vision models to compare")
    ap.add_argument("--out", type=pathlib.Path, help="where transcripts go (default: PAGES/transcripts)")
    a = ap.parse_args()

    if not a.pages.is_dir():
        sys.exit("no such folder: %s" % a.pages)
    pages = sorted(p for p in a.pages.iterdir() if p.suffix.lower() in EXTS)
    if not pages:
        sys.exit("no page photos (%s) in %s" % (", ".join(sorted(EXTS)), a.pages))
    out = a.out or a.pages / "transcripts"
    out.mkdir(parents=True, exist_ok=True)

    for page in pages:
        image = prepare(page)
        for model in a.models:
            dest = out / ("%s.%s.md" % (page.stem, model.replace(":", "-").replace("/", "-")))
            if dest.exists():
                print("skip  %s  %s (already done)" % (page.name, model))
                continue
            start = time.time()
            try:
                text = transcribe(model, image)
            except urllib.error.HTTPError as e:  # Ollama answered, e.g. the model isn't pulled yet
                print("FAIL  %s  %s: %s (try: ollama pull %s)" % (page.name, model, e, model))
                continue
            except urllib.error.URLError as e:
                sys.exit("Ollama isn't answering on 127.0.0.1:11434 (%s). Open the Ollama app, then rerun." % e)
            except Exception as e:
                print("FAIL  %s  %s: %s" % (page.name, model, e))
                continue
            dest.write_text("<!-- %s · %s -->\n\n%s\n" % (page.name, model, text))
            print("done  %s  %s  %d words  %.0fs" % (page.name, model, len(text.split()), time.time() - start))


if __name__ == "__main__":
    main()
