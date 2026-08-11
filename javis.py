"""
Project Codelander - Voice AI Agent (v2)
Features:
  1. Greets the user on launch.
  2. Asks "What would you like to do?" and listens for a voice command.
  3. Performs web searches (DuckDuckGo) and reads the top results aloud.
  4. Loops politely, asking "Anything else?" until the user says goodbye.

Stack:
  - STT: Vosk (offline)
  - TTS: Windows SAPI via pyttsx3 (offline)
  - Audio: sounddevice (PortAudio)
  - Search: requests + DuckDuckGo Instant Answer API

First-run: downloads ~50MB Vosk model to ./models/vosk-model-small-en-us-0.15
"""

import json
import os
import queue
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.request import urlretrieve

import pyttsx3
import sounddevice as sd
from vosk import KaldiRecognizer, Model

from search import NetworkError, ParseError, Searcher, SearchResult

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent
MODEL_DIR = APP_DIR / "models" / "vosk-model-en-us-0.22-lgraph"
MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip"
SAMPLE_RATE = 16000
BLOCK_SIZE = 1600  # 100ms chunks - smaller = less latency, more accurate end-of-speech
GREETING_HOUR_EVENING = 18
GREETING_HOUR_NIGHT = 22

# End-of-speech detection. After the user stops talking, wait this long
# before finalizing the transcript. 700ms is enough to bridge natural pauses
# mid-sentence ("search for...weather in Tokyo") without making the user wait
# noticeably between commands.
END_OF_SPEECH_SILENCE_SEC = 0.7
# Minimum amount of speech before we accept silence as "the user is done".
# Without this, a brief mic tick can fire EndOfSpeech before any words arrive.
MIN_SPEECH_SEC = 0.25
# Minimum cumulative loud audio before a transcript is trusted. Vosk sometimes
# guesses a short filler word from room noise or TTS tail audio.
MIN_ACCEPTED_VOICE_SEC = 0.35
# Loudness threshold (RMS over int16 samples) for "speech present".
# Tuned for typical laptop mics at normal speaking volume. Lower = more
# sensitive (catches quiet speech but more false positives); higher = only
# loud speech triggers the detector.
SILENCE_RMS_THRESHOLD = 600

AGENT_NAME = "Codelander"
USER_NAME = os.environ.get("", "CODELANDER")


# ---------------------------------------------------------------------------
# TTS - Windows SAPI (offline)
# ---------------------------------------------------------------------------
class Speaker:
    """Thin wrapper around pyttsx3 with a clean shutdown."""

    def __init__(self):
        self.engine = pyttsx3.init("sapi5")
        voices = self.engine.getProperty("voices")
        # Prefer a male English voice (David / Mark) if available
        for v in voices:
            if "english" in v.name.lower() and ("david" in v.name.lower() or "mark" in v.name.lower() or "guy" in v.name.lower()):
                self.engine.setProperty("voice", v.id)
                break
        else:
            # Fall back to first English voice
            for v in voices:
                if "english" in v.name.lower():
                    self.engine.setProperty("voice", v.id)
                    break
        self.engine.setProperty("rate", 175)  # slightly faster than default
        self.engine.setProperty("volume", 0.95)

    def say(self, text: str):
        print(f"[{AGENT_NAME}] {text}")
        self.engine.say(text)
        self.engine.runAndWait()

    def shutdown(self):
        try:
            self.engine.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# STT - Vosk (offline)
# ---------------------------------------------------------------------------
class Listener:
    """Captures mic audio and yields transcribed text via Vosk.

    End-of-speech detection is done by measuring RMS amplitude in the
    incoming audio rather than by relying on Vosk's `AcceptWaveform`
    boolean. Vosk can commit to a finalized hypothesis mid-sentence on
    the small model, so the old approach was prone to dropping the tail
    of an utterance and leaving the recognizer guessing. Waiting for
    actual silence (and a minimum amount of speech first) keeps the
    recognizer's hypothesis open until the user is genuinely done.
    """

    def __init__(self, model: Model):
        self.model = model
        self.recognizer = KaldiRecognizer(model, SAMPLE_RATE)
        self.recognizer.SetWords(True)
        self.audio_q: "queue.Queue[bytes]" = queue.Queue()

    def _callback(self, indata, _frames, _time_info, status):
        # sounddevice requires the 4-arg callback signature; we only use indata.
        del _frames, _time_info
        if status:
            print(f"[audio] {status}", file=sys.stderr)
        self.audio_q.put(bytes(indata))

    @staticmethod
    def _rms(data: bytes) -> float:
        """Root-mean-square amplitude of a 16-bit PCM buffer."""
        if not data:
            return 0.0
        # 2 bytes per sample, int16 little-endian
        n = len(data) // 2
        if n == 0:
            return 0.0
        total = 0
        for i in range(n):
            sample = int.from_bytes(data[i * 2 : i * 2 + 2], "little", signed=True)
            total += sample * sample
        return (total / n) ** 0.5

    def listen_once(self, timeout: float = 8.0) -> str:
        """Listen until the user finishes speaking, or `timeout` (seconds)."""
        self.recognizer = KaldiRecognizer(self.model, SAMPLE_RATE)
        self.recognizer.SetWords(True)
        deadline = time.time() + timeout
        # Drain any stale audio from a previous call.
        while not self.audio_q.empty():
            try:
                self.audio_q.get_nowait()
            except queue.Empty:
                break

        result_text = ""
        last_partial = ""
        speech_started_at: float | None = None
        last_voice_at: float | None = None
        voiced_seconds = 0.0
        block_seconds = BLOCK_SIZE / SAMPLE_RATE

        with sd.RawInputStream(
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            dtype="int16",
            channels=1,
            callback=self._callback,
        ):
            print(f"[{AGENT_NAME}] ...listening...")
            while time.time() < deadline:
                try:
                    data = self.audio_q.get(timeout=0.1)
                except queue.Empty:
                    # No audio in this window - check end-of-speech condition.
                    if (
                        speech_started_at is not None
                        and last_voice_at is not None
                        and (time.time() - last_voice_at) >= END_OF_SPEECH_SILENCE_SEC
                    ):
                        break
                    # Keep the recognizer warm even during silence.
                    self.recognizer.AcceptWaveform(b"")
                    continue

                amplitude = self._rms(data)
                now = time.time()
                if amplitude >= SILENCE_RMS_THRESHOLD:
                    if speech_started_at is None:
                        speech_started_at = now
                    last_voice_at = now
                    voiced_seconds += block_seconds

                # Feed the audio regardless of amplitude so quiet words aren't lost.
                self.recognizer.AcceptWaveform(data)

                # Live partial feedback (overwrites the same line).
                try:
                    partial = json.loads(self.recognizer.PartialResult()).get("partial", "").strip()
                except (ValueError, json.JSONDecodeError):
                    partial = ""
                if partial and partial != last_partial:
                    print(f"  >> {partial}", end="\r", flush=True)
                    last_partial = partial

                # If we've heard enough speech followed by enough silence, stop.
                if (
                    speech_started_at is not None
                    and (now - speech_started_at) >= MIN_SPEECH_SEC
                    and last_voice_at is not None
                    and (now - last_voice_at) >= END_OF_SPEECH_SILENCE_SEC
                ):
                    break

            # Finalize whatever's in the recognizer's hypothesis.
            try:
                final = json.loads(self.recognizer.FinalResult())
            except (ValueError, json.JSONDecodeError):
                final = {}
            result_text = (final.get("text") or "").strip()
            # Clear the partial line.
            if last_partial:
                print(" " * max(len(last_partial) + 4, 40), end="\r")

        if voiced_seconds < MIN_ACCEPTED_VOICE_SEC:
            return ""
        return result_text


# ---------------------------------------------------------------------------
# Model bootstrap
# ---------------------------------------------------------------------------
def ensure_model() -> Model:
    """Download the Vosk model on first run."""
    if MODEL_DIR.exists():
        return Model(str(MODEL_DIR))

    print(f"[{AGENT_NAME}] First run - downloading speech model (~50MB)...")
    MODEL_DIR.parent.mkdir(parents=True, exist_ok=True)
    zip_path = MODEL_DIR.parent / "vosk-model.zip"

    urlretrieve(MODEL_URL, zip_path)
    print(f"[{AGENT_NAME}] Extracting model...")
    with zipfile.ZipFile(zip_path, "r") as z:
        z.extractall(MODEL_DIR.parent)
    zip_path.unlink()
    return Model(str(MODEL_DIR))


# ---------------------------------------------------------------------------
# Core agent
# ---------------------------------------------------------------------------
def greeting_for_hour(hour: int) -> str:
    if hour < 12:
        return "Good morning"
    if hour < GREETING_HOUR_EVENING:
        return "Good afternoon"
    if hour < GREETING_HOUR_NIGHT:
        return "Good evening"
    return "Working late, I see"


# ---------------------------------------------------------------------------
# Command router
# ---------------------------------------------------------------------------
# Phrases that mean "I want to leave". Matched whole-utterance (after strip).
EXIT_PHRASES = (
    "goodbye",
    "good bye",
    "exit",
    "quit",
    "stop",
    "that's all",
    "thats all",
    "nothing",
    "no thanks",
    "no thank you",
    "bye",
    "no",
    "nope",
    "nah",
    "done",
    "finished",
)

# Phrases that mean "yes, keep listening — what's the next command?"
# These should NOT be dispatched to commands and should NOT exit.
# They just trigger another listen cycle.
AFFIRMATIVE_PHRASES = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "sure",
    "ok",
    "okay",
    "alright",
)

FOLLOWUP_TIMEOUT = 5.0
INITIAL_TIMEOUT = 10.0
MAX_QUERY_LEN = 80
IGNORED_UTTERANCES = {
    "a",
    "an",
    "the",
    "uh",
    "um",
    "er",
    "ah",
    "hmm",
}


# Regex patterns for search triggers. Order matters: longer phrases first.
# Capture group 1 is the search query.
#
# Each pattern allows:
#   - Optional wake word prefix: "hey", "codelander", "okay", "um", "uh"
#   - Optional filler words: "can you", "please", "i want to", "the"
#   - Vosk-friendly trigger spellings (search/serge/sarge/such all match)
#   - Tolerant whitespace
SEARCH_PATTERNS = (
    # "search for X" / "search about X"
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:i\s+want\s+(?:you\s+)?to\s+|can\s+you\s+|could\s+you\s+|please\s+|the\s+)?"
        r"(?:search|serge|sarge|such)\s+(?:for|about|info\s+on|information\s+on)\s+"
        r"(.+)$",
        re.IGNORECASE,
    ),
    # "look up X"
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|please\s+|the\s+)?"
        r"look\s+up\s+(.+)$",
        re.IGNORECASE,
    ),
    # "google X"
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|please\s+|the\s+)?"
        r"google\s+(.+)$",
        re.IGNORECASE,
    ),
    # "find X" / "find me X" / "find X online"
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|please\s+|the\s+)?"
        r"find\s+(?:me\s+|out\s+)?(.+?)(?:\s+online|\s+on\s+the\s+internet|\s+please)?$",
        re.IGNORECASE,
    ),
    # Bare "search X" — last because it's the most permissive
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|please\s+|the\s+)?"
        r"(?:search|serge|sarge|such)\s+(.+)$",
        re.IGNORECASE,
    ),
)

STOPWORDS = {"a", "an", "the", "it", "is", "and", "or", "of", "to", "in", "on", "for", "about", "info", "information", "please", "thanks"}


@dataclass
class Command:
    """A handlable voice command. Registry-based routing."""

    name: str
    extract_query: Callable[[str], Optional[str]]
    handle: Callable[[str, "Speaker"], None]


def extract_search_query(text: str) -> Optional[str]:
    """Return the search query if `text` is a search command, else None."""
    if not text:
        return None
    cleaned = text.strip().rstrip(".?!")
    for pattern in SEARCH_PATTERNS:
        m = pattern.match(cleaned)
        if m:
            query = m.group(1).strip()
            # Strip trailing filler like "please", "thanks"
            query = _strip_trailing_filler(query)
            if len(query) < 2:
                return None
            tokens = [t for t in query.split() if t.lower() not in STOPWORDS]
            if not tokens:
                return None
            return query
    return None


TRAILING_FILLER = ("please", "thanks", "thank you", "now", "right now")


def _strip_trailing_filler(query: str) -> str:
    """Remove polite filler words that the user appends after the query."""
    changed = True
    while changed:
        changed = False
        lower = query.lower().rstrip()
        for f in TRAILING_FILLER:
            suffix = " " + f
            if lower.endswith(suffix):
                query = query[: -len(suffix)].rstrip()
                changed = True
                break
    return query


def format_search_results(results: list[SearchResult], *, max_results: int = 3) -> str:
    """Take top N results and format them as a single speakable string."""
    if not results:
        return ""
    top = results[:max_results]
    chunks = []
    for i, r in enumerate(top, start=1):
        title = " ".join(r.title.split())
        snippet = r.snippet
        snippet = " ".join(snippet.split())  # collapse whitespace
        if len(snippet) > 200:
            snippet = snippet[:200].rsplit(" ", 1)[0] + "..."
        words = {1: "first", 2: "second", 3: "third"}
        label = words.get(i, f"result {i}")
        if title and title.lower() not in snippet.lower():
            chunks.append(f"{label}: {title}. {snippet}")
        else:
            chunks.append(f"{label}: {snippet}")
    return " ".join(chunks)


def handle_search_query(query: str, speaker: "Speaker") -> None:
    """Speak top search results for `query`. Never raises."""
    print(f"[search] query='{query}'")
    speaker.say(f"Searching the internet for {query}.")
    searcher = Searcher()
    try:
        results = searcher.search(query, max_results=5)
    except ValueError:
        speaker.say("What would you like me to search for?")
        return
    except NetworkError:
        speaker.say("I couldn't reach the internet. Please check your connection.")
        return
    except ParseError:
        speaker.say("The search results came back in a format I don't recognize.")
        return
    except Exception as exc:  # noqa: BLE001 — last-resort safety net
        print(f"[search] unexpected error: {exc}")
        speaker.say("Something went wrong with the search.")
        return

    if not results:
        speaker.say(f"I didn't find anything for {query}.")
        return

    spoken = format_search_results(results, max_results=3)
    speaker.say(f"Here is what I found for {query}. {spoken}")


# ---------------------------------------------------------------------------
# Close-program command
# ---------------------------------------------------------------------------
CLOSE_PROGRAM_PATTERNS = (
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|i\s+want\s+you\s+to\s+|the\s+)?"
        r"(?:close|shut\s+down|terminate|end|stop|kill|quit|exit)\s+"
        r"(?:the\s+)?(?:program|app|application|agent|assistant|codelander|codelander|process|session|yourself)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|i\s+want\s+you\s+to\s+|the\s+)?"
        r"(?:close|shut\s+down|terminate|end|stop|kill|quit|exit)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:hey\s+|okay\s+|um\s+|uh\s+|please\s+)?"
        r"(?:codelander[, ]+\s*)?"
        r"(?:can\s+you\s+|could\s+you\s+|please\s+|i\s+want\s+(?:you\s+)?to\s+)?"
        r"(?:power\s+down|go\s+sleep|sign\s+off)$",
        re.IGNORECASE,
    ),
)


def extract_close_program_query(text: str) -> Optional[str]:
    """Return 'close' if `text` is a close-the-program command, else None."""
    if not text:
        return None
    cleaned = text.strip().rstrip(".?!")
    for pattern in CLOSE_PROGRAM_PATTERNS:
        if pattern.match(cleaned):
            return "close"
    return None


def handle_close_program(query: str, speaker: "Speaker") -> None:
    """Speak a polite farewell. Caller is responsible for exiting the loop."""
    print(f"[close] shutting down")
    speaker.say("Shutting down. Goodbye.")


# Module-level command registry. Add new commands here.
COMMANDS: list[Command] = [
    Command(
        name="search",
        extract_query=extract_search_query,
        handle=handle_search_query,
    ),
    Command(
        name="close_program",
        extract_query=extract_close_program_query,
        handle=handle_close_program,
    ),
]


def route(utterance: str, speaker: "Speaker") -> Optional[Command]:
    """Dispatch `utterance` to the first matching command.

    Returns the dispatched Command if handled, else None.
    """
    if not utterance:
        return None
    for cmd in COMMANDS:
        query = cmd.extract_query(utterance)
        if query is not None:
            cmd.handle(query, speaker)
            return cmd
    return None


def is_exit_phrase(text: str) -> bool:
    if not text:
        return False
    cleaned = text.strip().lower().rstrip(".?!")
    return cleaned in EXIT_PHRASES


def is_affirmative_phrase(text: str) -> bool:
    """Bare 'yes / yeah / sure / ok' with no command payload."""
    if not text:
        return False
    cleaned = text.strip().lower().rstrip(".?!")
    return cleaned in AFFIRMATIVE_PHRASES


def is_ignored_utterance(text: str) -> bool:
    """Return True for recognizer noise that should not count as user input."""
    if not text:
        return False
    cleaned = text.strip().lower().rstrip(".?!")
    return cleaned in IGNORED_UTTERANCES


def listen_for_command(listener: Listener, timeout: float) -> str:
    """Listen until a meaningful utterance arrives or the timeout expires."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        utterance = listener.listen_once(timeout=max(0.1, deadline - time.time()))
        if not utterance:
            if time.time() < deadline:
                continue
            return ""
        if is_ignored_utterance(utterance):
            print(f"[noise] ignored transcript: {utterance!r}")
            continue
        return utterance
    return ""


def main():
    print(f"=== {AGENT_NAME} starting ===")
    model = ensure_model()
    speaker = Speaker()
    listener = Listener(model)

    try:
        # 1) Greet the user
        hour = time.localtime().tm_hour
        greeting = greeting_for_hour(hour)
        speaker.say(f"{greeting}, {USER_NAME}. {AGENT_NAME} online and at your service.")

        # 2) Main interaction loop
        speaker.say("What would you like to do?")
        first_turn = True
        utterance = listen_for_command(listener, timeout=INITIAL_TIMEOUT)

        while True:
            if not utterance:
                speaker.say("Standing by. Goodbye.")
                break

            print(f"[user] {utterance}")

            if is_exit_phrase(utterance):
                speaker.say("Goodbye.")
                break

            # On follow-up turns, "yes / yeah / sure" with no payload
            # means "go ahead" but isn't itself a search command.
            if not first_turn and is_affirmative_phrase(utterance):
                speaker.say("Sure. What would you like me to do?")
                utterance = listen_for_command(listener, timeout=FOLLOWUP_TIMEOUT)
                first_turn = False
                continue

            dispatched = route(utterance, speaker)
            if dispatched:
                # If the dispatched command was "close_program", exit the loop
                # so the speaker can shut down cleanly.
                if dispatched.name == "close_program":
                    break
            else:
                speaker.say("I don't know how to do that yet, but I will learn soon.")

            # Follow-up prompt
            speaker.say("Anything else?")
            utterance = listen_for_command(listener, timeout=FOLLOWUP_TIMEOUT)
            first_turn = False

    except KeyboardInterrupt:
        print(f"\n[{AGENT_NAME}] Interrupted.")
    finally:
        speaker.shutdown()
        print(f"=== {AGENT_NAME} offline ===")


if __name__ == "__main__":
    main()
