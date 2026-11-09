#!/usr/bin/env python3
"""Journal Scanner: snap journal pages as you flip, save them to the encrypted
vault, and transcribe them locally. The camera is either the iPhone running the
scanner page in Safari (phone mode) or a camera the Mac sees (Continuity Camera).

    python3 journal_scanner.py                       # vault: /Volumes/JournalVault/journals
    python3 journal_scanner.py --out ~/some/folder   # anywhere else
    python3 journal_scanner.py --model qwen2.5vl:7b  # faster, rougher transcripts

It opens http://127.0.0.1:8770 in the Mac's browser. Phone mode also listens
on https://<this-mac>.local:8771 for the iPhone: TLS with a local certificate,
and every request needs the pairing token from the QR code on the Mac. Pages
travel phone -> Mac on the home network only, and go only to Ollama on
127.0.0.1. The terminal log shows file names, word counts and timings, never
page text. Standard-library Python only.
"""
import argparse
import hmac
import json
import os
import secrets
import socket
import ssl
import subprocess
import tempfile
import pathlib
import queue
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transcribe_journal import PROMPT  # noqa: E402  (same folder)

DESCRIBE_PROMPT = """Describe the layout and visual content of this handwritten journal page. Do not transcribe the writing.
Use short bullet points:
- Layout: how the page is organised (main text, columns, lists, boxes, headings, dates, margin notes, arrows) and the reading order.
- Direction: which way each block of writing runs (e.g. "main text horizontal; a note written sideways up the right margin; one line upside down at the bottom").
- Drawings: each drawing, doodle, diagram or symbol: what it shows, where on the page, roughly how big, and how it relates to nearby text.
- Anything else that matters for reading the page (crossings-out, highlighting, stuck-in items, torn or faded areas).
If the page is a two-page spread, describe left and right separately."""

MERGE_PROMPT = """You are correcting the transcription of one handwritten journal page.

DRAFT is what a handwriting-reading model got from the photo: right layout, but some words misread, and [?] marks words it could not read.
SPOKEN is the writer reading the same page aloud, turned into text by speech recognition. The writer knows what they wrote, so SPOKEN decides which WORDS are on the page. But speech recognition can't hear layout or spelling: it guesses punctuation, spells numbers and symbols out, and picks sound-alikes (e.g. "cues" for "Q's", "e-books" for "eBooks").

Return JSON with one field, "transcript": DRAFT, corrected.
- Keep DRAFT's line breaks, order, numbering, symbols ($, ->, /), abbreviations, capitals and spelling wherever DRAFT's word sounds like what was said.
- Change a word only where SPOKEN shows DRAFT misread it (it sounds different). Fill [?] from SPOKEN.
- Add lines DRAFT missed, where they were read. Don't polish grammar.
- Keep every DRAFT line the writer didn't read aloud (e.g. a page they skipped), unchanged, with its [Left page]/[Right page] label.
- If DRAFT says "(no draft yet)", write SPOKEN out as the page text, one sentence or item per line.
- SPOKEN may hold marks the writer placed while reading: <u>words</u> means those words are underlined on the page, and a line with only --- is a line drawn across the page. A line starting "- " is a bullet point (whatever symbol the writer drew), and ★ marks something the writer starred. Keep all these marks, at the matching place in the transcript. Punctuation in SPOKEN was said aloud on purpose: where DRAFT has none, use SPOKEN's.

DRAFT:
%s

SPOKEN:
%s"""

MERGE_MODEL = "gpt-oss:20b"
DEFAULT_MODEL = "qwen3-vl:8b-instruct"
# Photo size sent to the reader. Scored on a real page (2026-11-06): qwen3-vl:8b-instruct read
# 83% of words right at 1400px vs 75% at 2000px, in half the time.
READ_PX = 1400


def prepare(path, px=None):
    """The page as a JPEG at most READ_PX on its long side (sips ships with macOS)."""
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "page.jpg"
        subprocess.run(["sips", "-s", "format", "jpeg", "-Z", str(px or READ_PX), str(path), "--out", str(out)],
                       check=True, capture_output=True)
        return out.read_bytes()
WHISPER = shutil.which("whisper-cli") or "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL = pathlib.Path.home() / "Library/Application Support/JournalScanner/whisper/ggml-large-v3-turbo.bin"
READ_ALOUD = "your reading"   # the "model" name shown for transcripts corrected by your reading
SUBDIRS = ("transcripts", "descriptions", "readings", "notes", "crops")
PAGEFINDER = pathlib.Path(__file__).resolve().parent / "scanner_app" / "pagefinder"
NARRATE_UI = pathlib.Path(__file__).resolve().parent / "scanner_ui" / "narrate.html"
ORGANIZE_UI = pathlib.Path(__file__).resolve().parent / "scanner_ui" / "organize.html"
# Where a page's own files live, besides the photo (all named p0005.* or p0005.md).
PAGE_PLACES = SUBDIRS + (".thumbs", "_superseded/transcripts", "_superseded/descriptions")
CROP_RE = re.compile(r"^(p\d{4})\.(\d)\.jpg$")
CHUNK_RE = re.compile(r"^\d{4}\.wav$")
# A narrate page: a whole photo ("p0003") or one half of a two-page spread ("p0002.1" left, "p0002.2" right).
SIDE_RE = re.compile(r"^(p\d{4})(?:\.([12]))?$")
SIDES = {"1": "Left", "2": "Right"}
TRAINING_GOAL = 200   # pages read aloud before training the handwriting model (see CUSTOMIZE.md)


def draft_side(text, part):
    """The [Left page] / [Right page] section of a model reading (the whole text if it has none)."""
    if not part:
        return text
    m = re.search(r"\[%s page\]\s*\n(.*?)(?=\n\[(?:Left|Right) page\]|\Z)" % SIDES[part], text, re.S)
    return m.group(1).strip() if m else text


class LiveSpeech:
    """whisper.cpp's server, kept running so short pieces of speech turn into text in about a
    second. Local only (127.0.0.1); its own output (which includes the text) goes nowhere."""
    def __init__(self):
        self.proc, self.port, self.lock = None, None, threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        server = shutil.which("whisper-server") or "/opt/homebrew/bin/whisper-server"
        if not WHISPER_MODEL.exists():
            raise RuntimeError("the speech model is still downloading")
        with socket.socket() as so:
            so.bind(("127.0.0.1", 0))
            self.port = so.getsockname()[1]
        self.proc = subprocess.Popen([server, "-m", str(WHISPER_MODEL), "--host", "127.0.0.1", "--port", str(self.port),
                                      "-l", "en", "-nt"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import atexit
        atexit.register(lambda p=self.proc: p.poll() is None and p.terminate())
        for _ in range(120):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    print("live speech ready", flush=True)
                    return
            except OSError:
                time.sleep(0.25)
        raise RuntimeError("the live speech model didn't start")

    def text(self, wav, prompt="", wait=True):
        """WAV bytes -> text. With wait=False, returns None if busy (for previews)."""
        if not self.lock.acquire(blocking=wait):
            return None
        try:
            self.start()
            b = "----js%s" % secrets.token_hex(8)
            fields = {"response_format": "json", "temperature": "0", "prompt": prompt[-400:]}
            body = b"".join(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (b, k, v)).encode()
                            for k, v in fields.items())
            body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                     "Content-Type: audio/wav\r\n\r\n" % b).encode() + wav + ("\r\n--%s--\r\n" % b).encode()
            req = urllib.request.Request("http://127.0.0.1:%d/inference" % self.port, data=body,
                                         headers={"Content-Type": "multipart/form-data; boundary=" + b})
            with urllib.request.urlopen(req, timeout=120) as r:
                out = json.load(r).get("text", "")
            # whisper marks silence and noises in brackets; drop those
            out = re.sub(r"\[[^\]]*\]|\((?:music|silence|inaudible|blank_audio)[^)]*\)", " ", out, flags=re.I)
            return re.sub(r"\s+", " ", out).strip()
        finally:
            self.lock.release()


def join_wavs(files, out):
    """Glue 16 kHz mono 16-bit WAV pieces into one WAV."""
    import struct
    def data(raw):  # the sound itself, past whatever header chunks the WAV has
        i = 12
        while i + 8 <= len(raw):
            tag, size = raw[i:i + 4], struct.unpack("<I", raw[i + 4:i + 8])[0]
            if tag == b"data":
                return raw[i + 8:i + 8 + size]
            i += 8 + size + (size & 1)
        return b""
    pcm = b"".join(data(f.read_bytes()) for f in files)
    head = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) \
        + b"data" + struct.pack("<I", len(pcm))
    out.write_bytes(head + pcm)
_WORDS = None


def english_share(text):
    """Share of words in the macOS word list: high when a page was read the right way up."""
    global _WORDS
    if _WORDS is None:
        try:
            _WORDS = set(w.strip().lower() for w in open("/usr/share/dict/words"))
        except OSError:
            _WORDS = set()
    words = [w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3]
    return sum(w in _WORDS for w in words) / len(words) if words else 0.0


UPRIGHT_PROMPT = ("This picture shows the same handwritten page twice: on the LEFT (A) and on the RIGHT (B), turned "
                  "differently. In one of them the handwriting is the right way up, so it could be read normally. Which one: A or B?")


def which_upright(model, image):
    import base64
    body = {"model": model, "prompt": UPRIGHT_PROMPT, "stream": False, "think": False, "keep_alive": "30m",
            "format": {"type": "object", "properties": {"answer": {"type": "string", "enum": ["A", "B"]}}, "required": ["answer"]},
            "images": [base64.b64encode(image).decode()], "options": {"temperature": 0, "num_ctx": 8192, "num_predict": 20}}
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(json.load(r)["response"]).get("answer")


def ensure_pagefinder():
    """Build the page finder from its Swift source the first time (or after it changes)."""
    src = PAGEFINDER.with_suffix(".swift")
    if not PAGEFINDER.exists() or PAGEFINDER.stat().st_mtime < src.stat().st_mtime:
        subprocess.run(["swiftc", "-O", str(src), "-o", str(PAGEFINDER)], check=True, capture_output=True, timeout=600)


def find_pages(src, crops_dir, model):
    """Crop the photo to the paper, turn the writing upright, split two-page spreads.
    Returns the page images to read (falls back to the whole photo if anything fails)."""
    stem = src.stem
    done = sorted(crops_dir.glob(stem + ".[0-9].jpg")) if crops_dir.is_dir() else []
    if done:
        return done, None
    try:
        ensure_pagefinder()
        info = json.loads(subprocess.run([str(PAGEFINDER), "analyze", str(src)], check=True, capture_output=True,
                                         text=True, timeout=120).stdout)
        box = [str(v) for v in info["box"]]
        # Which way up? Show the model both possible turns side by side and ask which is upright.
        # Asked twice with the order swapped; if the answers disagree, leave the page as photographed.
        # (Asked alone, it can't tell; compared side by side it got 8/8 in testing, 2026-11-06.)
        first, second = (0, 180) if info["lines"] == "across" else (270, 90)
        with tempfile.TemporaryDirectory() as d:
            picks = []
            for a, b in ((first, second), (second, first)):
                pair = pathlib.Path(d) / ("pair%d.jpg" % a)
                subprocess.run([str(PAGEFINDER), "pair", str(src)] + box + [str(a), str(b), str(pair)],
                               check=True, capture_output=True, timeout=120)
                picks.append(a if which_upright(model, prepare(pair)) == "A" else b)
        turn = picks[0] if picks[0] == picks[1] else first
        crops_dir.mkdir(exist_ok=True)
        out = json.loads(subprocess.run([str(PAGEFINDER), "cut", str(src), str(crops_dir), stem] + box + [str(turn)],
                                        check=True, capture_output=True, text=True, timeout=120).stdout)
        return sorted(crops_dir.glob(stem + ".[0-9].jpg")), {"turn": turn, "pages": out["pages"], "lines": info["lines"]}
    except Exception as e:
        print("page finder skipped for %s: %s" % (src.name, str(e)[:120]), flush=True)
        return [src], None

HERE = pathlib.Path(__file__).resolve().parent
UI = HERE / "scanner_ui" / "index.html"
OLLAMA = "http://127.0.0.1:11434"  # local only, never a remote host
DEFAULT_OUT = pathlib.Path(os.environ.get("JOURNAL_SCANNER_OUT") or "/Volumes/JournalVault/journals")  # see CUSTOMIZE.md
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")
PAGE_RE = re.compile(r"^p(\d{4})\.jpg$")
MAX_UPLOAD = 40 * 1024 * 1024
STATE = pathlib.Path.home() / "Library" / "Application Support" / "JournalScanner"
COOKIE = "jst"


# ---------- phone mode: local certificate authority, pairing token, QR ----------
def openssl():
    for c in (shutil.which("openssl"), "/opt/homebrew/bin/openssl", "/usr/bin/openssl"):
        if c and os.path.exists(c):
            return c
    raise RuntimeError("openssl not found")


def lan_ips():
    ips = []
    for dev in ("en0", "en1", "en2", "en3"):
        r = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            ips.append(r.stdout.strip())
    return ips


def lan_host():
    r = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True)
    return (r.stdout.strip() or socket.gethostname().split(".")[0]) + ".local"


def ensure_certs(host, ips):
    """A private CA (made once; the iPhone trusts it once) signs a server cert for
    this Mac's .local name and current IPs, remade whenever those change."""
    STATE.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    ca_key, ca_crt = STATE / "ca.key", STATE / "ca.crt"
    key, crt, san_file = STATE / "server.key", STATE / "server.crt", STATE / "server.san"
    run = lambda *a: subprocess.run([openssl(), *a], check=True, capture_output=True)
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        if not (ca_key.exists() and ca_crt.exists()):
            (d / "ca.cnf").write_text(
                "[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=Journal Scanner (this Mac only)\n"
                "[v3]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
            run("req", "-x509", "-new", "-nodes", "-newkey", "rsa:2048", "-sha256", "-days", "3650",
                "-keyout", str(ca_key), "-out", str(ca_crt), "-config", str(d / "ca.cnf"))
            os.chmod(ca_key, 0o600)
            for f in (key, crt, san_file):  # a new CA means a new server cert
                f.unlink(missing_ok=True)
        san = ",".join(["DNS:" + host, "DNS:localhost"] + ["IP:" + ip for ip in ips])
        if crt.exists() and key.exists() and san_file.exists() and san_file.read_text() == san:
            return ca_crt, crt, key
        (d / "ext.cnf").write_text("basicConstraints=CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
                                    "extendedKeyUsage=serverAuth\nsubjectAltName=%s\n" % san)
        run("req", "-new", "-nodes", "-newkey", "rsa:2048", "-keyout", str(key), "-out", str(d / "s.csr"),
            "-subj", "/CN=" + host)
        os.chmod(key, 0o600)
        run("x509", "-req", "-in", str(d / "s.csr"), "-CA", str(ca_crt), "-CAkey", str(ca_key), "-CAcreateserial",
            "-out", str(crt), "-days", "800", "-sha256", "-extfile", str(d / "ext.cnf"))
        san_file.write_text(san)
    return ca_crt, crt, key


def pair_token():
    STATE.mkdir(parents=True, exist_ok=True)
    f = STATE / "pair-token"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(18))
        os.chmod(f, 0o600)
    return f.read_text().strip()


def qr_png(text):
    """QR code PNG from macOS CoreImage (via JavaScript for Automation); no network, no libraries."""
    with tempfile.TemporaryDirectory() as d:
        out = pathlib.Path(d) / "qr.png"
        js = """
ObjC.import('CoreImage'); ObjC.import('AppKit');
function run(argv) {
  const f = $.CIFilter.filterWithName('CIQRCodeGenerator');
  f.setValueForKey($(argv[0]).dataUsingEncoding($.NSUTF8StringEncoding), 'inputMessage');
  f.setValueForKey($('M'), 'inputCorrectionLevel');
  const rep = $.NSBitmapImageRep.alloc.initWithCIImage(f.outputImage);
  rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $()).writeToFileAtomically(argv[1], true);
}"""
        subprocess.run(["osascript", "-l", "JavaScript", "-e", js, text, str(out)], check=True, capture_output=True)
        return out.read_bytes()


NOTE_START = re.compile(r"(?:^|(?<=[.!?,]\s)|(?<=^\s))\b(?:note|side note|comment)\b[\s,.:;-]*", re.I)
NOTE_END = re.compile(r"\b(?:end (?:of )?note|back to the page|reading again|continuing)\b[\s,.:;-]*", re.I)


NOTE_MARK = re.compile(r"\[note:\s*(.*?)\s*(?:\]|$)", re.I | re.S)


def split_bracket_notes(text):
    """Narrate shows notes as [note: ...] while you talk; take them out of the page text."""
    notes = [n.strip() for n in NOTE_MARK.findall(text) if n.strip()]
    rest = NOTE_MARK.sub(" ", text)
    rest = "\n".join(re.sub(r"[ \t]{2,}", " ", line).strip() for line in rest.split("\n"))
    return re.sub(r"\n{2,}", "\n", rest).strip(), "\n\n".join(notes)


def split_notes(spoken):
    """Separate what was read from the page and what was said about it.
    A note starts at the word "note" (at the start of a sentence) and runs until
    "end note" / "back to the page", or the end of the recording."""
    reading, notes, pos = [], [], 0
    while True:
        m = NOTE_START.search(spoken, pos)
        if not m:
            reading.append(spoken[pos:])
            break
        reading.append(spoken[pos:m.start()])
        e = NOTE_END.search(spoken, m.end())
        notes.append(spoken[m.end():e.start() if e else len(spoken)].strip())
        if not e:
            break
        pos = e.end()
    return " ".join(r.strip() for r in reading if r.strip()), "\n\n".join(n for n in notes if n)


def load_settings():
    try:
        return json.loads((STATE / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def save_settings(**kv):
    STATE.mkdir(parents=True, exist_ok=True)
    data = load_settings()
    data.update(kv)
    (STATE / "settings.json").write_text(json.dumps(data, indent=1))


def model_tag(model):
    return model.replace(":", "-").replace("/", "-")


class Scanner:
    def __init__(self, out, model):
        self.out = out
        self.model = model
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.voice_jobs = queue.Queue()  # read-aloud recordings, handled before page reading
        self.state = {}  # (journal, page) -> {"status", "words", "secs", "error"}
        self.voice_state = {}  # (journal, page) -> "transcribing" | "merging" | "done" | "failed: ..."
        self.current = None
        self.reading_on = load_settings().get("reading", True)  # off = scan only; pages wait to be read
        self.wake = threading.Event()
        self.speech = LiveSpeech()
        threading.Thread(target=self.worker, daemon=True).start()

    def set_reading(self, on):
        self.reading_on = bool(on)
        save_settings(reading=self.reading_on)
        if on:
            self.wake.set()
        print("reading %s" % ("on" if on else "paused"), flush=True)

    # ---------- storage ----------
    def ready(self):
        # Once the vault (the parent folder) is mounted, make the journals folder in it.
        if not self.out.is_dir() and self.out.parent.is_dir() and self.out.parent != pathlib.Path("/Volumes"):
            try:
                self.out.mkdir()
                print("made %s" % self.out, flush=True)
            except OSError:
                return False
        return self.out.is_dir()

    def journal_dir(self, journal):
        if not NAME_RE.match(journal or "") or journal.startswith("_"):
            raise ValueError("journal names use letters, numbers, spaces, - _ . (up to 80)")
        return self.out / journal

    def transcript_path(self, journal, page):
        return self.out / journal / "transcripts" / ("%s.%s.md" % (page[:-4], model_tag(self.model)))

    def description_path(self, journal, page):
        return self.out / journal / "descriptions" / ("%s.%s.md" % (page[:-4], model_tag(self.model)))

    def page_files(self, journal, page):
        """Everything made from a page: transcripts, descriptions, recordings, notes."""
        d, stem = self.out / journal, page[:-4]
        out = []
        for sub in SUBDIRS:
            if (d / sub).is_dir():
                out += [f for f in (d / sub).iterdir() if f.name == stem + ".md" or f.name.startswith(stem + ".")]
        return out

    def page_info(self, journal, page):
        """All readings of one page: every model's transcript, descriptions, your notes."""
        d, stem = self.journal_dir(journal), page[:-4]
        def read_all(sub):
            rows = []
            if (d / sub).is_dir():
                for t in sorted((d / sub).glob(stem + ".*.md")):
                    raw = t.read_text()
                    m = re.match(r"<!--.*?·\s*(.+?)\s*-->", raw)
                    rows.append({"model": m.group(1) if m else t.name[len(stem) + 1:-3], "text": strip_header(raw)})
            return rows
        notes = d / "notes" / (stem + ".md")
        return {"transcripts": [r for r in read_all("transcripts") if not r["model"].endswith(".spoken")],
                "descriptions": read_all("descriptions"),
                "notes": notes.read_text() if notes.exists() else "",
                "voice": self.voice_state.get((journal, page))}

    def journals(self):
        if not self.ready():
            return []
        rows = []
        for d in sorted(self.out.iterdir()):
            if d.is_dir() and NAME_RE.match(d.name) and not d.name.startswith("_"):
                pages = [p for p in d.iterdir() if PAGE_RE.match(p.name)]
                rows.append({"name": d.name, "pages": len(pages),
                             "modified": max([p.stat().st_mtime for p in pages] or [d.stat().st_mtime])})
        rows.sort(key=lambda r: -r["modified"])
        return rows

    def pages(self, journal):
        d = self.journal_dir(journal)
        if not d.is_dir():
            return []
        rows = []
        for p in sorted(d.iterdir()):
            if not PAGE_RE.match(p.name):
                continue
            st = dict(self.state.get((journal, p.name)) or {})
            t = self.transcript_path(journal, p.name)
            if t.exists() and st.get("status") != "reading":
                st["status"] = "done"
                st.setdefault("words", len(strip_header(t.read_text()).split()))
            st.setdefault("status", "waiting")
            st["spoken"] = (d / "transcripts").is_dir() and any((d / "transcripts").glob(p.name[:-4] + "*.read-aloud.md"))
            rows.append(dict(name=p.name, number=int(PAGE_RE.match(p.name).group(1)),
                             taken=p.stat().st_mtime, **st))
        return rows

    def save(self, journal, data):
        if not self.ready():
            raise RuntimeError("the save folder isn't there (is your vault mounted?)")
        if not data.startswith(b"\xff\xd8"):
            raise ValueError("expected a JPEG")
        d = self.journal_dir(journal)
        d.mkdir(exist_ok=True)
        with self.lock:
            nums = [int(m.group(1)) for m in (PAGE_RE.match(p.name) for p in d.iterdir()) if m]
            discarded = d / "_discarded"
            if discarded.is_dir():
                nums += [int(m.group(1)) for m in (PAGE_RE.match(p.name) for p in discarded.iterdir()) if m]
            name = "p%04d.jpg" % (max(nums or [0]) + 1)
            tmp = d / (name + ".part")
            tmp.write_bytes(data)
            tmp.rename(d / name)
            self.state[(journal, name)] = {"status": "queued"}
        self.jobs.put((journal, name))
        print("saved %s/%s  %d KB" % (journal, name, len(data) // 1024), flush=True)
        return name

    # ---------- organizing: thumbnails, new / renamed journals, moving pages between journals ----------
    def thumb(self, journal, page):
        """A small copy of the page photo for the organizer (kept in the journal's .thumbs folder)."""
        if not PAGE_RE.match(page):
            raise ValueError("bad page name")
        d = self.journal_dir(journal)
        src, out = d / page, d / ".thumbs" / page
        if not src.exists():
            raise FileNotFoundError(page)
        if not out.exists() or out.stat().st_mtime < src.stat().st_mtime:
            out.parent.mkdir(exist_ok=True)
            subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "70", "-Z", "420", str(src), "--out", str(out)],
                           check=True, capture_output=True)
        return out.read_bytes()

    def new_journal(self, name):
        if not self.ready():
            raise RuntimeError("the save folder isn't there (is your vault mounted?)")
        d = self.journal_dir((name or "").strip())
        if d.exists():
            raise ValueError("there's already a journal called that")
        d.mkdir()
        print("new journal %s" % d.name, flush=True)
        return d.name

    def rename_journal(self, old, new):
        src, dest = self.journal_dir(old), self.journal_dir((new or "").strip())
        if not src.is_dir():
            raise FileNotFoundError(old)
        if dest.exists():
            raise ValueError("there's already a journal called that")
        with self.lock:
            if self.current and self.current[0] == old:
                raise RuntimeError("a page of this journal is being read right now; try again in a few seconds")
            src.rename(dest)
            for table in (self.state, self.voice_state):
                for k in [k for k in table if k[0] == old]:
                    table[(dest.name, k[1])] = table.pop(k)
        self.log_move("renamed journal %s -> %s" % (old, dest.name))
        n = self.resume()  # pages still waiting to be read are queued again under the new name
        print("renamed journal %s -> %s (%d queued)" % (old, dest.name, n), flush=True)
        return dest.name

    def log_move(self, line):
        with open(self.out / "_moves.log", "a") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))

    def move_pages(self, src_journal, pages, dest_journal):
        """Move photos and everything made from them (readings, transcripts, recordings, notes, crops)
        to another journal. They're added after its last page, in their current order, and renamed to
        fit its numbering (p0005 -> p0108, p0005.1.read-aloud.md -> p0108.1.read-aloud.md, ...)."""
        sd, dd = self.journal_dir(src_journal), self.journal_dir(dest_journal)
        if sd == dd:
            raise ValueError("pick a different journal to move them to")
        if not dd.is_dir():
            raise FileNotFoundError(dest_journal)
        pages = sorted(set(pages or []))
        if not pages or any(not PAGE_RE.match(p) or not (sd / p).exists() for p in pages):
            raise ValueError("some of those pages aren't in %s" % src_journal)
        moved = []
        with self.lock:
            for p in pages:
                stem = p[:-4]
                if self.current == (src_journal, p):
                    raise RuntimeError("page %d is being read right now; try again in a few seconds" % int(stem[1:]))
                busy = [v for (j, sid), v in self.voice_state.items() if j == src_journal and sid.split(".")[0] == stem
                        and v in ("waiting", "transcribing", "merging")]
                if busy:
                    raise RuntimeError("page %d's reading is still being saved; try again in a few seconds" % int(stem[1:]))
            nums = [int(m.group(1)) for m in (PAGE_RE.match(x.name) for x in dd.iterdir()) if m]
            if (dd / "_discarded").is_dir():
                nums += [int(m.group(1)) for m in (PAGE_RE.match(x.name) for x in (dd / "_discarded").iterdir()) if m]
            n = max(nums or [0])
            for p in pages:
                n += 1
                old, new = p[:-4], "p%04d" % n
                for rel in PAGE_PLACES:
                    folder = sd / rel
                    if not folder.is_dir():
                        continue
                    for f in sorted(folder.iterdir()):
                        if f.name == old + ".md" or f.name.startswith(old + "."):
                            target = dd / rel / (new + f.name[len(old):])
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.move(str(f), str(target))
                            if target.suffix == ".md":  # the header names the photo
                                t = target.read_text()
                                target.write_text(t.replace("<!-- %s.jpg" % old, "<!-- %s.jpg" % new, 1))
                shutil.move(str(sd / p), str(dd / (new + ".jpg")))
                for table in (self.state, self.voice_state):
                    for k in [k for k in table if k[0] == src_journal and k[1].split(".")[0] == old]:
                        table.pop(k)
                moved.append([p, new + ".jpg"])
                self.log_move("%s/%s -> %s/%s.jpg" % (src_journal, p, dd.name, new))
        queued = 0
        for _, new in moved:  # pages that weren't read yet get read in their new journal
            if not (self.transcript_path(dd.name, new).exists() and self.description_path(dd.name, new).exists()):
                self.state[(dd.name, new)] = {"status": "queued"}
                self.jobs.put((dd.name, new))
                queued += 1
        print("moved %d page(s) %s -> %s  (%d to read)" % (len(moved), src_journal, dd.name, queued), flush=True)
        return moved

    def discard(self, journal, page):
        """Move a page (and its transcript) aside into _discarded/. Nothing is deleted."""
        if not PAGE_RE.match(page):
            raise ValueError("bad page name")
        d = self.journal_dir(journal)
        src = d / page
        if not src.exists():
            raise FileNotFoundError(page)
        bin_ = d / "_discarded"
        bin_.mkdir(exist_ok=True)
        for t in self.page_files(journal, page):
            sub = bin_ / t.parent.name
            sub.mkdir(exist_ok=True)
            shutil.move(str(t), str(sub / t.name))
        shutil.move(str(src), str(bin_ / page))
        self.state[(journal, page)] = {"status": "discarded"}
        print("moved aside %s/%s" % (journal, page), flush=True)

    def delete(self, journal, page):
        """Delete a page for good: its photo and every transcript of it. Asked for from the app, with a confirm."""
        if not PAGE_RE.match(page):
            raise ValueError("bad page name")
        d = self.journal_dir(journal)
        src = d / page
        if not src.exists():
            raise FileNotFoundError(page)
        for t in self.page_files(journal, page):
            t.unlink()
        src.unlink()
        self.state[(journal, page)] = {"status": "deleted"}
        print("deleted %s/%s" % (journal, page), flush=True)

    def rotate(self, journal, page, degrees):
        """Turn the saved photo (90 = clockwise) and read it again, since sideways text reads badly."""
        if not PAGE_RE.match(page) or degrees not in (90, 180, 270):
            raise ValueError("bad rotation")
        src = self.journal_dir(journal) / page
        if not src.exists():
            raise FileNotFoundError(page)
        tmp = src.with_suffix(".rot.jpg")
        subprocess.run(["sips", "-r", str(degrees), str(src), "--out", str(tmp)], check=True, capture_output=True)
        tmp.replace(src)
        # every model's reading of the sideways photo is now stale; your reading aloud stays
        d, stem = self.out / journal, page[:-4]
        for sub in ("transcripts", "descriptions", "crops"):
            for t in (d / sub).glob(stem + ".*") if (d / sub).is_dir() else []:
                if not t.name.endswith(".read-aloud.md"):
                    t.unlink()
        self.state[(journal, page)] = {"status": "queued"}
        self.jobs.put((journal, page))
        print("rotated %s/%s by %d" % (journal, page, degrees), flush=True)

    def retry(self, journal, page):
        for t in [self.transcript_path(journal, page), self.description_path(journal, page)] + \
                sorted((self.out / journal / "crops").glob(page[:-4] + ".*.jpg")):
            if t.exists():
                t.unlink()  # this model's reading and the page crops are redone; the photo and your reading stay
        self.state[(journal, page)] = {"status": "queued"}
        self.jobs.put((journal, page))

    def resume(self):
        """Queue every saved page that has no transcript from the current model yet."""
        n = 0
        for j in self.journals():
            for p in self.pages(j["name"]):
                if p["status"] in ("waiting", "failed") or not self.description_path(j["name"], p["name"]).exists():
                    self.state[(j["name"], p["name"])] = {"status": "queued"}
                    self.jobs.put((j["name"], p["name"]))
                    n += 1
        return n

    # ---------- transcription ----------
    def worker(self):
        while True:
            try:  # your read-aloud recordings go first: you're waiting on those
                self.do_voice(*self.voice_jobs.get_nowait())
                continue
            except queue.Empty:
                pass
            try:
                journal, page = self.jobs.get(timeout=1)
            except queue.Empty:
                continue
            if not self.reading_on:  # paused: keep the page in line, in order
                self.jobs.put((journal, page))
                self.wake.wait(2)
                self.wake.clear()
                continue
            self.do_page(journal, page)

    def do_page(self, journal, page):
        src = self.out / journal / page
        dest, desc = self.transcript_path(journal, page), self.description_path(journal, page)
        if not src.exists() or (dest.exists() and desc.exists()):
            return
        self.state[(journal, page)] = {"status": "reading"}
        self.current = (journal, page)
        start = time.time()
        try:
            parts, how = find_pages(src, self.out / journal / "crops", self.model)
            if how:
                print("pages  %s/%s  %d page(s), turned %d" % (journal, page, how["pages"], how["turn"]), flush=True)
            images = [prepare(p) for p in parts]
            label = (lambda i: "[%s page]\n" % ("Left", "Right")[i]) if len(images) == 2 else (lambda i: "")
            side = (lambda i: " %s" % ("left", "right")[i]) if len(images) == 2 else (lambda i: "")
            read = lambda prompt: "\n\n".join(
                label(i) + ollama_transcribe(self.model, im, prompt, where="%s/%s%s" % (journal, page, side(i))).strip()
                for i, im in enumerate(images))
            if not dest.exists():
                text = read(PROMPT)
                if not src.exists():  # deleted or moved aside while being read
                    return
                dest.parent.mkdir(exist_ok=True)
                dest.write_text("<!-- %s · %s -->\n\n%s\n" % (page, self.model, text))
                print("read  %s/%s  %d words  %.0fs" % (journal, page, len(text.split()), time.time() - start), flush=True)
            if not desc.exists():
                t1 = time.time()
                about = read(DESCRIBE_PROMPT)
                if not src.exists():
                    return
                desc.parent.mkdir(exist_ok=True)
                desc.write_text("<!-- %s · %s -->\n\n%s\n" % (page, self.model, about))
                print("described  %s/%s  %.0fs" % (journal, page, time.time() - t1), flush=True)
            words = len(strip_header(dest.read_text()).split())
            self.state[(journal, page)] = {"status": "done", "words": words, "secs": round(time.time() - start)}
        except urllib.error.URLError as e:
            if isinstance(e, urllib.error.HTTPError):
                self.fail(journal, page, e)
            else:  # Ollama is closed: keep the page in line and try again shortly
                self.state[(journal, page)] = {"status": "queued", "error": "waiting for Ollama"}
                time.sleep(10)
                self.jobs.put((journal, page))
        except Exception as e:  # keep going; the page can be retried from the app
            self.fail(journal, page, e)
        finally:
            self.current = None

    # ---------- reading aloud ----------
    def save_reading(self, journal, page, wav):
        if not PAGE_RE.match(page) or not (self.journal_dir(journal) / page).exists():
            raise ValueError("no such page")
        if not (wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"):
            raise ValueError("expected a WAV recording")
        d = self.out / journal / "readings"
        d.mkdir(exist_ok=True)
        f = d / ("%s.%s.wav" % (page[:-4], time.strftime("%Y%m%d-%H%M%S")))
        f.write_bytes(wav)
        self.voice_state[(journal, page)] = "waiting"
        self.voice_jobs.put((journal, page, f))
        print("recorded  %s/%s  %d KB" % (journal, page, len(wav) // 1024), flush=True)

    def do_voice(self, journal, page, wav, spoken=None, part=None):
        """Your reading -> text (whisper.cpp, local) -> merged with the draft (local model) -> transcript + notes.
        From the narrate page the text was already heard live (spoken is given)."""
        stem, d = page[:-4], self.out / journal
        sid = stem + ("." + part if part else "")   # one side of a spread, or the whole photo
        key = (journal, sid if part or spoken is not None else page)
        from_narrate = spoken is not None
        try:
            if spoken is None:
                self.voice_state[key] = "transcribing"
                if not WHISPER_MODEL.exists():
                    raise RuntimeError("the speech model is still downloading")
                t0 = time.time()
                base = wav.with_suffix("")
                subprocess.run([WHISPER, "-m", str(WHISPER_MODEL), "-f", str(wav), "-l", "en", "-nt", "-np",
                                "-otxt", "-of", str(base)], check=True, capture_output=True, timeout=1800)
                spoken = pathlib.Path(str(base) + ".txt").read_text().strip()
                pathlib.Path(str(base) + ".txt").rename(base.with_name(base.name + ".spoken.md"))
                print("heard  %s/%s  %d words  %.0fs" % (journal, page, len(spoken.split()), time.time() - t0), flush=True)
            if not spoken:
                raise RuntimeError("no speech found in the recording")

            self.voice_state[key] = "merging"
            drafts = [t for t in [self.transcript_path(journal, page)] + sorted((d / "transcripts").glob(stem + ".*.md"))
                      if t.exists() and not t.name.endswith(".read-aloud.md") and not re.match(r"p\d{4}\.\d\.", t.name)]
            draft = draft_side(strip_header(drafts[0].read_text()), part) if drafts else "(no draft yet)"
            t1 = time.time()
            spoken_page, spoken_notes = split_bracket_notes(spoken) if "[note:" in spoken.lower() or from_narrate else split_notes(spoken)
            body = {"model": MERGE_MODEL, "prompt": MERGE_PROMPT % (draft, spoken_page), "stream": False,
                    # Narrate text already has your punctuation and symbols, so a light check is enough:
                    # ~3 s a page. "medium" thought ~3,000 words first: 30-45 s, minutes on a busy page.
                    "keep_alive": "10m", "think": "low" if from_narrate else "medium", "options": {"temperature": 0},
                    "format": {"type": "object", "properties": {"transcript": {"type": "string"}},
                               "required": ["transcript"]}}
            req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=1800) as r:
                out = json.loads(json.load(r)["response"])
            text, notes = out.get("transcript", "").strip(), spoken_notes.strip()
            (d / "transcripts").mkdir(exist_ok=True)
            label = READ_ALOUD + (" (%s page)" % SIDES[part].lower() if part else "")
            (d / "transcripts" / (sid + ".read-aloud.md")).write_text("<!-- %s · %s -->\n\n%s\n" % (page, label, text))
            if notes:
                (d / "notes").mkdir(exist_ok=True)
                with open(d / "notes" / (stem + ".md"), "a") as f:
                    f.write("### %s%s\n\n%s\n\n" % (time.strftime("%Y-%m-%d %H:%M"), " (%s page)" % SIDES[part].lower() if part else "", notes))
            self.voice_state[key] = "done"
            print("merged  %s/%s  %d words, %d note words  %.0fs" % (journal, page, len(text.split()), len(notes.split()), time.time() - t1), flush=True)
        except Exception as e:
            msg = describe_error(e, MERGE_MODEL) if isinstance(e, urllib.error.URLError) else str(e)[:200]
            self.voice_state[key] = "failed: " + msg
            print("FAIL reading  %s/%s  %s" % (journal, page, msg), flush=True)

    # ---------- narrate: talk through the pages, text appears live, saved as you go ----------
    def side(self, journal, sid):
        """'p0002.1' -> ('p0002.jpg', '1'); 'p0003' -> ('p0003.jpg', None). The photo must exist."""
        m = SIDE_RE.match(sid or "")
        if not m or not (self.journal_dir(journal) / (m.group(1) + ".jpg")).exists():
            raise ValueError("no such page")
        return m.group(1) + ".jpg", m.group(2)

    def live_dir(self, journal, sid):
        self.side(journal, sid)
        return self.out / journal / "readings" / (sid + ".live")

    def narrate_pages(self, journal):
        """Every page in reading order: each half of a two-page spread is its own page.
        Sides the reader found blank get no page number."""
        d = self.journal_dir(journal)
        rows, number = [], 0
        for p in self.pages(journal):
            stem = p["name"][:-4]
            crops = sorted(c.name for c in (d / "crops").glob(stem + ".[0-9].jpg")) if (d / "crops").is_dir() else []
            model = self.transcript_path(journal, p["name"])
            reading = strip_header(model.read_text()) if model.exists() else None
            sides = [(c[len(stem) + 1], c) for c in crops] if len(crops) == 2 else [(None, crops[0] if crops else None)]
            for part, crop in sides:
                sid = stem + ("." + part if part else "")
                live = self.out / journal / "readings" / (sid + ".live")
                said = sorted((d / "readings").glob(sid + ".[0-9]*.spoken.md")) if (d / "readings").is_dir() else []
                words = None if reading is None else [w for w in draft_side(reading, part).split() if w not in ("[?]", "(blank)")]
                blank = words is not None and len(words) <= 1
                if not blank:
                    number += 1
                rows.append({"id": sid, "photo": p["name"], "photoNumber": p["number"], "side": SIDES.get(part),
                             "crop": crop, "number": None if blank else number, "blank": blank,
                             "draft": (live / "draft.txt").read_text() if (live / "draft.txt").exists() else "",
                             "said": said[-1].read_text().strip() if said else "",
                             "spoken": (d / "transcripts" / (sid + ".read-aloud.md")).exists(),
                             "voice": self.voice_state.get((journal, sid))})
        return rows

    def narrate_totals(self):
        """Pages read aloud in every journal: the handwriting training set."""
        rows = []
        for j in self.journals():
            t = self.out / j["name"] / "transcripts"
            n = len(list(t.glob("*.read-aloud.md"))) if t.is_dir() else 0
            if n:
                rows.append({"name": j["name"], "pages": n})
        return {"total": sum(r["pages"] for r in rows), "goal": TRAINING_GOAL, "journals": rows}

    def narrate_chunk(self, journal, page, wav, prompt):
        """One stretch of speech: kept as audio first (so nothing is lost), then turned into text."""
        if not (wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"):
            raise ValueError("expected a WAV recording")
        live = self.live_dir(journal, page)
        live.mkdir(parents=True, exist_ok=True)
        with self.lock:
            n = len([f for f in live.iterdir() if CHUNK_RE.match(f.name)]) + 1
            (live / ("%04d.wav" % n)).write_bytes(wav)
        draft = live / "draft.txt"
        # The command words lead the hint so whisper expects them ("new line", not "Noo line").
        hint = "New line. Underlined. Gold star. List item. Divider. Note. End note. Period. Comma. Quote. Parenthesis. Slash.\n"
        # Only the command words: with the page so far in the hint, whisper skipped words you said
        # again that matched it (journals repeat lines).
        text = self.speech.text(wav, hint + (prompt or ""))
        if text:  # kept on the Mac straight away too, in case the page closes before it saves
            with self.lock:
                old = draft.read_text() if draft.exists() else ""
                self.narrate_draft(journal, page, (old + (" " if old and not old.endswith(("\n", " ")) else "") + text))
        return text

    def narrate_draft(self, journal, page, text):
        live = self.live_dir(journal, page)
        live.mkdir(parents=True, exist_ok=True)
        tmp = live / "draft.txt.part"
        tmp.write_text(text)
        tmp.rename(live / "draft.txt")

    def narrate_commit(self, journal, page, text):
        """Done with a page: one recording + what was said go into readings/, then the usual merge."""
        live, text = self.live_dir(journal, page), (text or "").strip()
        if not text:
            raise ValueError("nothing was said for this page")
        d = self.out / journal / "readings"
        d.mkdir(exist_ok=True)
        base = d / ("%s.%s" % (page, time.strftime("%Y%m%d-%H%M%S")))
        pieces = sorted(f for f in live.iterdir() if CHUNK_RE.match(f.name)) if live.is_dir() else []
        wav = base.with_name(base.name + ".wav")
        if pieces:
            join_wavs(pieces, wav)
        base.with_name(base.name + ".spoken.md").write_text(text + "\n")
        if live.is_dir():
            shutil.rmtree(live)  # its pieces now live in the one recording above
        photo, part = self.side(journal, page)
        self.voice_state[(journal, page)] = "waiting"
        self.voice_jobs.put((journal, photo, wav, text, part))
        self.wake.set()
        print("narrated  %s/%s  %d words, %d piece(s)" % (journal, page, len(text.split()), len(pieces)), flush=True)

    def fail(self, journal, page, e):
        msg = describe_error(e, self.model)
        self.state[(journal, page)] = {"status": "failed", "error": msg}
        print("FAIL  %s/%s  %s" % (journal, page, msg), flush=True)


LOOP_NOTE = "[the reading got stuck repeating here; the rest of this page wasn't read]"


def find_loop(text, n=8, times=10, window=200):
    """Where a stuck repeat starts, or None. Looks at the last `window` words for one `n`-word
    phrase said `times` or more times (handwriting doesn't do that; a stuck model does).
    Returns the character offset of the phrase's second appearance, so the first stays."""
    spans = [m.span() for m in re.finditer(r"\S+", text)]
    if len(spans) < n * times:
        return None
    words = [text[a:b].lower() for a, b in spans]
    tail0 = max(0, len(words) - window)
    seen = {}
    for i in range(tail0, len(words) - n + 1):
        seen.setdefault(tuple(words[i:i + n]), []).append(i)
    gram, where = max(seen.items(), key=lambda kv: len(kv[1]))
    if len(where) < times:
        return None
    first = next(i for i in range(len(words) - n + 1) if tuple(words[i:i + n]) == gram)
    later = [i for i in range(first + n, len(words) - n + 1) if tuple(words[i:i + n]) == gram]
    if not later:
        return None
    cut = spans[later[0]][0]
    nl = text.rfind("\n", spans[first][0], cut)   # end on a whole line when the loop is line by line
    return nl if nl > 0 else cut


def _generate(body, check_loop):
    """One streamed request. Stops reading (which stops Ollama) as soon as a loop shows up.
    Returns (text, looped)."""
    req = urllib.request.Request(OLLAMA + "/api/generate", data=json.dumps(dict(body, stream=True)).encode(),
                                 headers={"Content-Type": "application/json"})
    parts, n = [], 0
    with urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("error"):
                raise RuntimeError(msg["error"])
            parts.append(msg.get("response", ""))
            n += 1
            if check_loop and n % 40 == 0:   # every ~40 tokens
                text = "".join(parts)
                cut = find_loop(text)
                if cut is not None:
                    return text[:cut].rstrip(), True
            if msg.get("done"):
                break
    text = "".join(parts)
    cut = find_loop(text) if check_loop else None
    return (text[:cut].rstrip(), True) if cut is not None else (text, False)


def ollama_transcribe(model, image, prompt=PROMPT, num_predict=2000, where=""):
    import base64
    req_body = {"model": model, "prompt": prompt, "keep_alive": "30m",
                "images": [base64.b64encode(image).decode()],
                # 8k context: a page's image + answer fit, and the 32b models then fit fully in
                # graphics memory (32k spilled 12% onto the CPU and ran 2x slower).
                # A light repeat penalty stops most "same line forever" loops on messy handwriting.
                "options": {"temperature": 0, "num_ctx": 8192, "num_predict": num_predict,
                            "repeat_penalty": 1.05, "repeat_last_n": 128}}
    # Qwen3-style models "think" first by default, which can use up the whole answer: turn it off.
    for think in (False, None):
        body = dict(req_body, think=think) if think is not None else req_body
        try:
            text, looped = _generate(body, True)
            if looped:
                # About 3 in 100 pages still got stuck (2026-11-08). It's stopped as soon as it
                # starts; try once more with a stronger penalty and keep whichever got further.
                print("loop  %s  stopped after %d words; reading again" % (where, len(text.split())), flush=True)
                strong = dict(body, options=dict(body["options"], repeat_penalty=1.2, repeat_last_n=256))
                text2, looped2 = _generate(strong, True)
                if not looped2 or len(text2) > len(text):
                    text, looped = text2, looped2
                print("loop  %s  second try %s, %d words" % (where, "stuck again" if looped else "fine", len(text.split())), flush=True)
                if looped:
                    text = text.rstrip() + "\n\n" + LOOP_NOTE
            return text.strip()
        except urllib.error.HTTPError as e:
            if think is not None and e.code == 400:  # a model that doesn't take the think option
                continue
            raise


def describe_error(e, model):
    if isinstance(e, urllib.error.HTTPError):
        return "Ollama said %s (is %s pulled?)" % (e.code, model)
    if isinstance(e, urllib.error.URLError):
        return "Ollama isn't answering. Open the Ollama app."
    return str(e)[:200]


_cam_check = {"t": 0.0, "v": None}
_cam_lock = threading.Lock()


def _profiler(kind):
    try:
        out = subprocess.run(["/usr/sbin/system_profiler", kind, "-json"], capture_output=True, timeout=8).stdout
        return json.loads(out or b"{}")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def _names(node):
    """Every "_name" in a system_profiler tree."""
    if isinstance(node, dict):
        if "_name" in node:
            yield str(node["_name"])
        for v in node.values():
            yield from _names(v)
    elif isinstance(node, list):
        for v in node:
            yield from _names(v)


class PhoneState:
    """Follows macOS's own Continuity Camera log for the iPhone's readiness flags.
    On a Mac with no camera of its own, macOS only offers the iPhone as a camera when it is
    plugged in ("wired") or reports itself ready ("usable": locked, still, stood up), so this
    tells you which one is missing. Flags only; started on first use, stopped at exit."""
    RE = re.compile(r"magic:(\d) \(usable:(\d) nearby:(\d) wired:(\d)")

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.state = None   # {"usable", "nearby", "wired", "at"}
        self.ignored_at = 0  # last time macOS said it ignores the phone because Disconnect was tapped

    def start(self):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            import pty
            master, slave = pty.openpty()   # a terminal, so log stream sends each line at once
            try:
                self.proc = subprocess.Popen(
                    ["/usr/bin/log", "stream", "--style", "compact", "--predicate",
                     'process == "ContinuityCaptureAgent" AND (eventMessage CONTAINS "magic:" OR eventMessage CONTAINS "User Disconnect Mode")'],
                    stdout=slave, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            except OSError:
                os.close(master); os.close(slave)
                self.proc = None
                return
            os.close(slave)
            import atexit
            atexit.register(lambda p=self.proc: p.poll() is None and p.terminate())
            threading.Thread(target=self._read, args=(master,), daemon=True).start()

    def _read(self, fd):
        out = os.fdopen(fd, "r", errors="replace")
        for line in out:
            if "User Disconnect Mode" in line:
                self.ignored_at = time.time()
                continue
            m = self.RE.search(line)
            if m:
                _, usable, nearby, wired = (x == "1" for x in m.groups())
                self.state = {"usable": usable, "nearby": nearby, "wired": wired, "at": time.time()}

    def get(self):
        self.start()
        st = self.state
        if not st or time.time() - st["at"] > 180:
            return None
        out = {k: st[k] for k in ("usable", "nearby", "wired")}
        out["ignored"] = time.time() - self.ignored_at < 30
        return out

    def reconnect(self):
        """Restart macOS's Continuity Camera agent (it runs as this user; launchd starts it
        again at once). That clears "User Disconnect Mode", which tapping Disconnect on the
        iPhone leaves behind until the phone is plugged in."""
        r = subprocess.run(["/usr/bin/killall", "ContinuityCaptureAgent"], capture_output=True, timeout=10)
        self.ignored_at = 0
        self.state = None
        with _cam_lock:
            _cam_check["v"] = None
        return r.returncode == 0


PHONE_STATE = PhoneState()


def camera_check():
    """What macOS itself sees, so the page can tell "the Mac doesn't see the iPhone"
    apart from "the browser doesn't". Device names only. Cached for 2 s."""
    with _cam_lock:
        if _cam_check["v"] is not None and time.time() - _cam_check["t"] < 2:
            return _cam_check["v"]
        cams = list(_names(_profiler("SPCameraDataType").get("SPCameraDataType", [])))
        usb = list(_names(_profiler("SPUSBHostDataType"))) + list(_names(_profiler("SPUSBDataType")))
        bt = None
        for c in _profiler("SPBluetoothDataType").get("SPBluetoothDataType", []):
            st = (c.get("controller_properties") or {}).get("controller_state")
            if st:
                bt = st.endswith("_on")
        wifi = None
        try:
            ports = subprocess.run(["/usr/sbin/networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"Hardware Port: Wi-Fi\nDevice: (\S+)", ports)
            if m:
                pw = subprocess.run(["/usr/sbin/networksetup", "-getairportpower", m.group(1)], capture_output=True, text=True, timeout=5).stdout
                wifi = pw.strip().endswith("On")
        except (OSError, subprocess.TimeoutExpired):
            pass
        v = {"cameras": cams,
             "iphone": any(re.search(r"iphone|continuity", c, re.I) and not re.search(r"desk view", c, re.I) for c in cams),
             "usb_iphone": any(re.search(r"iphone", n, re.I) for n in usb),
             "bluetooth": bt, "wifi": wifi, "phone": PHONE_STATE.get()}
        _cam_check.update(t=time.time(), v=v)
        return v


def ollama_models():
    """Installed Ollama models that can see images, or None if Ollama isn't running."""
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=2) as r:
            names = [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None
    vision = []
    for n in names:
        try:
            req = urllib.request.Request(OLLAMA + "/api/show", data=json.dumps({"model": n}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as r:
                if "vision" in json.load(r).get("capabilities", []):
                    vision.append(n)
        except Exception:
            pass
    return vision


def strip_header(text):
    return re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)


class Handler(BaseHTTPRequestHandler):
    scanner = None  # set in main()

    def log_message(self, *a):  # quiet: no URLs (journal names) in the log
        pass

    def send(self, code, body=b"", ctype="application/json", cache="no-store"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code, msg):
        self.send(code, {"error": msg})

    lan = False       # True on the phone (HTTPS) listener
    token = None      # pairing token the phone must carry
    pair_url = None   # the URL the QR code holds

    def guard(self):
        if self.lan:
            return self.guard_lan()
        # Only this Mac's own pages may call the API (blocks other sites in the browser).
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self.fail(HTTPStatus.FORBIDDEN, "local only")
            return False
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            self.fail(HTTPStatus.FORBIDDEN, "local only")
            return False
        return True

    def guard_lan(self):
        """Phone listener: everything needs the pairing token (from the QR code, kept as a cookie)."""
        u = urlparse(self.path)
        if u.path == "/ca.crt":  # the public certificate, so Safari can trust this Mac
            self.send(200, (STATE / "ca.crt").read_bytes(), "application/x-x509-ca-cert")
            return False
        given = (parse_qs(u.query).get("pair") or [""])[0]
        if given and hmac.compare_digest(given, self.token):
            self.send_response(303)
            self.send_header("Set-Cookie", "%s=%s; Max-Age=31536000; Path=/; Secure; HttpOnly; SameSite=Lax" % (COOKIE, self.token))
            nxt = (parse_qs(u.query).get("next") or ["/"])[0]
            self.send_header("Location", nxt if nxt in ("/", "/narrate") else "/")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        cookies = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        if not hmac.compare_digest(cookies.get(COOKIE, ""), self.token):
            self.send(403, LOCKED_PAGE, "text/html; charset=utf-8")
            return False
        origin = self.headers.get("Origin")
        host = (self.headers.get("Host") or "").split(":")[0]
        if origin and urlparse(origin).hostname != host:
            self.fail(HTTPStatus.FORBIDDEN, "wrong origin")
            return False
        return True

    def parts(self):
        u = urlparse(self.path)
        return [unquote(p) for p in u.path.strip("/").split("/") if p], parse_qs(u.query)

    def do_GET(self):
        if not self.guard():
            return
        s = self.scanner
        p, q = self.parts()
        try:
            if not p:
                return self.send(200, UI.read_bytes(), "text/html; charset=utf-8")
            if p == ["organize"]:
                return self.send(200, ORGANIZE_UI.read_bytes(), "text/html; charset=utf-8")
            if len(p) == 3 and p[0] == "thumb" and PAGE_RE.match(p[2]):
                return self.send(200, s.thumb(p[1], p[2]), "image/jpeg", cache="private, max-age=600")
            if p == ["narrate"]:
                return self.send(200, NARRATE_UI.read_bytes(), "text/html; charset=utf-8")
            if p == ["api", "narrate-totals"]:
                return self.send(200, s.narrate_totals())
            if len(p) == 3 and p[:2] == ["api", "narrate"]:
                return self.send(200, s.narrate_pages(p[2]))
            if len(p) == 3 and p[0] == "crop" and CROP_RE.match(p[2]):
                f = s.journal_dir(p[1]) / "crops" / p[2]
                if not f.exists():
                    return self.fail(404, "no crop")
                return self.send(200, f.read_bytes(), "image/jpeg", cache="private, max-age=3600")
            if p == ["api", "camera-check"] and not self.lan:
                return self.send(200, camera_check())
            if p == ["api", "status"]:
                models = ollama_models()
                return self.send(200, {
                    "ready": s.ready(), "out": str(s.out), "model": s.model,
                    "ollama": models is not None, "models": models or [],
                    "queue": s.jobs.qsize(), "reading": list(s.current) if s.current else None,
                    "reading_on": s.reading_on})
            if p == ["api", "journals"]:
                return self.send(200, s.journals())
            if p == ["api", "pair"] and not self.lan:
                return self.send(200, {"url": self.pair_url, "ca": self.pair_url and self.pair_url.split("?")[0] + "ca.crt",
                                       "narrate": self.pair_url and self.pair_url + "&next=/narrate"})
            if p == ["pair.png"] and not self.lan and self.pair_url:
                if not hasattr(Handler, "_qr"):
                    Handler._qr = qr_png(self.pair_url)
                return self.send(200, Handler._qr, "image/png")
            if len(p) == 3 and p[:2] == ["api", "pages"]:
                return self.send(200, s.pages(p[2]))
            if len(p) == 3 and p[0] == "img" and PAGE_RE.match(p[2]):
                f = s.journal_dir(p[1]) / p[2]
                if not f.exists():
                    return self.fail(404, "no page")
                return self.send(200, f.read_bytes(), "image/jpeg", cache="private, max-age=86400")
            if len(p) == 4 and p[:2] == ["api", "page"] and PAGE_RE.match(p[3]):
                return self.send(200, s.page_info(p[2], p[3]))
            if len(p) == 4 and p[:2] == ["api", "transcripts"] and PAGE_RE.match(p[3]):
                # every model's reading of this page, for side-by-side comparison
                d = s.journal_dir(p[2]) / "transcripts"
                rows = []
                for t in sorted(d.glob(p[3][:-4] + ".*.md")) if d.is_dir() else []:
                    raw = t.read_text()
                    m = re.match(r"<!--.*?·\s*(.+?)\s*-->", raw)
                    rows.append({"model": m.group(1) if m else t.name.split(".", 1)[1][:-3], "text": strip_header(raw)})
                return self.send(200, rows)
            if len(p) == 4 and p[:2] == ["api", "transcript"] and PAGE_RE.match(p[3]):
                s.journal_dir(p[2])
                t = s.transcript_path(p[2], p[3])
                return self.send(200, {"text": strip_header(t.read_text()) if t.exists() else None})
            self.fail(404, "not found")
        except (ValueError, FileNotFoundError) as e:
            self.fail(400, str(e))
        except subprocess.CalledProcessError:
            self.fail(500, "couldn't make that picture")

    def do_POST(self):
        if not self.guard():
            return
        s = self.scanner
        p, q = self.parts()
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_UPLOAD:
                return self.fail(413, "too large")
            body = self.rfile.read(n)
            if p == ["api", "camera-reconnect"] and not self.lan:
                return self.send(200, {"ok": PHONE_STATE.reconnect()})
            if len(p) == 3 and p[:2] == ["api", "pages"]:
                return self.send(200, {"name": s.save(p[2], body)})
            if len(p) == 4 and p[:2] == ["api", "discard"]:
                s.discard(p[2], p[3])
                return self.send(200, {"ok": True})
            if len(p) == 5 and p[:2] == ["api", "rotate"] and p[4].isdigit():
                s.rotate(p[2], p[3], int(p[4]))
                return self.send(200, {"ok": True})
            if len(p) == 4 and p[:2] == ["api", "readaloud"]:
                s.save_reading(p[2], p[3], body)
                return self.send(200, {"ok": True})
            if len(p) == 5 and p[:2] == ["api", "narrate"]:
                what, journal, page = p[2], p[3], p[4]
                if what == "chunk":
                    return self.send(200, {"text": s.narrate_chunk(journal, page, body, "")})
                if what == "peek":  # a preview of what's being said; not kept
                    s.live_dir(journal, page)
                    if not (body[:4] == b"RIFF" and body[8:12] == b"WAVE"):
                        raise ValueError("expected a WAV recording")
                    return self.send(200, {"text": s.speech.text(body, "", wait=False)})
                if what == "draft":
                    with s.lock:
                        s.narrate_draft(journal, page, json.loads(body or b"{}").get("text", ""))
                    return self.send(200, {"ok": True})
                if what == "commit":
                    s.narrate_commit(journal, page, json.loads(body or b"{}").get("text", ""))
                    return self.send(200, {"ok": True})
            if p == ["api", "move"]:
                b = json.loads(body or b"{}")
                return self.send(200, {"moved": s.move_pages(b.get("from"), b.get("pages"), b.get("to"))})
            if p == ["api", "journal-new"]:
                return self.send(200, {"name": s.new_journal(json.loads(body or b"{}").get("name"))})
            if p == ["api", "journal-rename"]:
                b = json.loads(body or b"{}")
                return self.send(200, {"name": s.rename_journal(b.get("from"), b.get("to"))})
            if p == ["api", "narrate", "warm"]:
                s.speech.start()
                return self.send(200, {"ok": True})
            if len(p) == 4 and p[:2] == ["api", "delete"]:
                s.delete(p[2], p[3])
                return self.send(200, {"ok": True})
            if len(p) == 4 and p[:2] == ["api", "retry"]:
                s.retry(p[2], p[3])
                return self.send(200, {"ok": True})
            if p == ["api", "clientlog"]:  # camera diagnostics from the page (never page content)
                msg = re.sub(r"[^\x20-\x7e]", "?", body.decode("utf-8", "replace"))[:300]
                print("%s says: %s" % ("phone" if self.lan else "mac page", msg), flush=True)
                return self.send(200, {"ok": True})
            if p == ["api", "reading"]:
                s.set_reading(json.loads(body or b"{}").get("on", True))
                return self.send(200, {"reading_on": s.reading_on})
            if p == ["api", "model"]:
                model = json.loads(body or b"{}").get("model", "")
                if model not in (ollama_models() or []):
                    return self.fail(400, "not an installed vision model")
                s.model = model
                save_settings(model=model)
                print("model %s  (%d pages queued)" % (model, s.resume()), flush=True)
                return self.send(200, {"model": model})
            self.fail(404, "not found")
        except (ValueError, FileNotFoundError) as e:
            self.fail(400, str(e))
        except RuntimeError as e:
            self.fail(409, str(e))


LOCKED_PAGE = b"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Journal Scanner</title><body style="background:#15120f;color:#f1e9dc;font:17px system-ui;display:grid;place-items:center;height:90vh;text-align:center;padding:20px">
<div><h2 style="font-family:ui-serif,Georgia">Journal Scanner</h2><p>Scan the QR code shown on the Mac<br>(Journal Scanner &rarr; <b>iPhone</b> button) to open this.</p></div>"""


class PhoneServer(ThreadingHTTPServer):
    """HTTPS listener for the iPhone. The TLS handshake happens in each request's thread."""
    def __init__(self, addr, handler, ctx):
        super().__init__(addr, handler)
        self.ctx = ctx
        self.seen = set()

    def finish_request(self, request, client_address):
        try:
            request = self.ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            # e.g. Safari refusing the certificate before it's trusted
            print("phone %s: secure connection not made (%s)" % (client_address[0], getattr(e, "reason", None) or e), flush=True)
            return
        if client_address[0] not in self.seen:
            self.seen.add(client_address[0])
            print("phone %s connected" % client_address[0], flush=True)
        super().finish_request(request, client_address)


def start_phone_server(scanner, port):
    host, ips = lan_host(), lan_ips()
    ca, crt, key = ensure_certs(host, ips)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    token = pair_token()

    class PhoneHandler(Handler):
        lan = True
    PhoneHandler.token = token
    # The QR uses the Mac's IP address: phones can't always look up .local names.
    Handler.pair_url = "https://%s:%d/?pair=%s" % (ips[0] if ips else host, port, token)
    srv = PhoneServer(("0.0.0.0", port), PhoneHandler, ctx)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Phone mode on https://%s:%d  (also %s; pair from the iPhone button in the app)"
          % (ips[0] if ips else host, port, host), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT, help="folder that holds one folder per journal")
    ap.add_argument("--model", help="Ollama vision model for transcripts (default: the last one picked in the app)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-open", action="store_true", help="don't open the browser")
    ap.add_argument("--no-phone", action="store_true", help="don't listen for the iPhone on the home network")
    ap.add_argument("--phone-port", type=int, default=8771)
    a = ap.parse_args()

    import signal
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the live speech server is stopped too
    scanner = Scanner(a.out.expanduser(), a.model or load_settings().get("model") or DEFAULT_MODEL)
    Handler.scanner = scanner
    if scanner.ready():
        n = scanner.resume()
        if n:
            print("%d saved page(s) still need reading; queued them" % n)
    else:
        print("Note: %s isn't there yet. Mount your vault (or pass --out, or set JOURNAL_SCANNER_OUT); the app will notice." % a.out)
    if not a.no_phone:
        try:
            start_phone_server(scanner, a.phone_port)
        except Exception as e:  # the Mac-only scanner still works
            print("Phone mode is off: %s" % e, flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = "http://127.0.0.1:%d" % a.port
    print("Journal Scanner on %s  (Ctrl-C to stop)" % url, flush=True)
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
