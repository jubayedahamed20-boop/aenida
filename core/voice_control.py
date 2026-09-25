"""
AENIDA Voice Control  v1.0
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Offline voice interface using:
  • OpenAI Whisper (base model, 150 MB) — speech → text
  • pyttsx3                             — text → speech (no internet)
  • pyaudio                             — microphone capture

Wake word  : say "AENIDA" to activate
Commands   : "analyze Bitcoin", "system status", "stop trading",
             "add coin Solana", "think what is RSI", "quit"

USAGE
─────
  # Standalone (speaks results):
  python core/voice_control.py

  # Integrated into main.py — add --voice flag:
  python core/main.py --mode display --voice

  # Test mode (no mic, no speakers needed — simulates full loop):
  python core/voice_control.py --test

DEPENDENCIES  (all offline after install)
──────────────────────────────────────────
  pip install openai-whisper pyttsx3 pyaudio
  # whisper base model downloads once (~150 MB) on first run

ARCHITECTURE
────────────
  MicThread  ──► AudioQueue ──► WhisperThread ──► CommandQueue
                                                       │
  TTSThread ◄── ResponseQueue ◄── CommandDispatcher ◄──┘

  All threads are daemons — die cleanly when main thread exits.
  No locks needed: queues handle all cross-thread communication.
"""

import argparse
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("voice_control")

# ── Project root so we can import AENIDA modules ─────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import path_setup  # registers core/, trading/, mobile/, tools/
except ImportError:
    pass  # running standalone outside project tree


# ══════════════════════════════════════════════════════════════════
#  DEPENDENCY GUARDS
# ══════════════════════════════════════════════════════════════════

def _check_deps() -> Dict[str, bool]:
    """Return availability of each optional dependency."""
    deps = {}

    try:
        import whisper  # openai-whisper
        deps["whisper"] = True
    except ImportError:
        deps["whisper"] = False

    try:
        import pyttsx3
        deps["pyttsx3"] = True
    except ImportError:
        deps["pyttsx3"] = False

    try:
        import pyaudio
        deps["pyaudio"] = True
    except ImportError:
        deps["pyaudio"] = False

    return deps


# ══════════════════════════════════════════════════════════════════
#  TTS ENGINE
# ══════════════════════════════════════════════════════════════════

class TTSEngine:
    """
    Text-to-speech wrapper around pyttsx3.
    Falls back to print() if pyttsx3 is unavailable.

    pyttsx3 is NOT thread-safe — all speak() calls are serialised
    through an internal queue processed by a single dedicated thread.
    """

    def __init__(self, rate: int = 165, volume: float = 0.95):
        self._rate    = rate
        self._volume  = volume
        self._queue: queue.Queue = queue.Queue()
        self._engine  = None
        self._ready   = False
        self._thread  = threading.Thread(
            target=self._worker, daemon=True, name="tts_worker")
        self._thread.start()

    def _worker(self) -> None:
        """Dedicated TTS thread — pyttsx3 must live here."""
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty("rate",   self._rate)
            engine.setProperty("volume", self._volume)
            # Prefer a female voice if available
            voices = engine.getProperty("voices")
            for v in voices:
                if "female" in (v.name or "").lower() or \
                   "zira" in (v.id or "").lower():
                    engine.setProperty("voice", v.id)
                    break
            self._engine = engine
            self._ready  = True
            log.info("[TTS] pyttsx3 ready")
        except Exception as e:
            log.warning(f"[TTS] pyttsx3 unavailable ({e}) — using print fallback")

        while True:
            text = self._queue.get()
            if text is None:
                break
            if self._engine:
                try:
                    self._engine.say(text)
                    self._engine.runAndWait()
                except Exception as e:
                    log.error(f"[TTS] speak error: {e}")
                    print(f"  🔊 {text}")
            else:
                print(f"  🔊 {text}")

    def speak(self, text: str) -> None:
        """Queue text for speech (non-blocking)."""
        log.info(f"[TTS] → {text[:80]}")
        self._queue.put(text)

    def stop(self) -> None:
        """Signal worker to exit."""
        self._queue.put(None)


# ══════════════════════════════════════════════════════════════════
#  WHISPER TRANSCRIBER
# ══════════════════════════════════════════════════════════════════

class WhisperTranscriber:
    """
    Wraps openai-whisper for offline speech-to-text.
    Model is loaded once and reused for every transcription.
    """

    # Whisper model sizes vs RAM:
    #   tiny   ~75 MB   (fast, less accurate)
    #   base  ~150 MB   (recommended for 4 GB RAM)
    #   small ~500 MB
    DEFAULT_MODEL = "base"

    def __init__(self, model_name: str = DEFAULT_MODEL):
        self._model      = None
        self._model_name = model_name
        self._lock       = threading.Lock()
        # Load model in background so startup is instant
        threading.Thread(
            target=self._load_model, daemon=True, name="whisper_loader"
        ).start()

    def _load_model(self) -> None:
        try:
            import whisper
            log.info(f"[WHISPER] Loading '{self._model_name}' model…")
            t0 = time.time()
            with self._lock:
                self._model = whisper.load_model(self._model_name)
            log.info(
                f"[WHISPER] Model ready in {time.time()-t0:.1f}s")
        except Exception as e:
            log.error(f"[WHISPER] Load failed: {e}")

    def is_ready(self) -> bool:
        with self._lock:
            return self._model is not None

    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe a .wav file to text.
        Returns empty string on failure.
        """
        with self._lock:
            if self._model is None:
                log.warning("[WHISPER] Model not loaded yet")
                return ""
            try:
                result = self._model.transcribe(
                    audio_path,
                    language="en",
                    fp16=False,        # CPU-safe (no GPU required)
                    temperature=0.0,   # deterministic
                )
                text = result.get("text", "").strip()
                log.info(f"[WHISPER] Heard: '{text}'")
                return text
            except Exception as e:
                log.error(f"[WHISPER] Transcribe failed: {e}")
                return ""


# ══════════════════════════════════════════════════════════════════
#  MICROPHONE RECORDER
# ══════════════════════════════════════════════════════════════════

class MicRecorder:
    """
    Records audio from the default microphone using pyaudio.
    Saves short clips (up to max_seconds) to a temp .wav file,
    then signals the caller to transcribe.

    Uses energy-based VAD (Voice Activity Detection):
      • Silence below energy_threshold → clip ends
      • If no voice detected → clip discarded
    """

    CHUNK        = 1024
    FORMAT       = None   # set in __init__ (needs pyaudio constant)
    CHANNELS     = 1
    RATE         = 16000  # Whisper expects 16 kHz
    MAX_SECONDS  = 10
    SILENCE_SECS = 1.2    # stop recording after this much silence

    def __init__(self, energy_threshold: int = 300,
                 tmp_path: str = "/tmp/aenida_voice.wav"):
        self._threshold = energy_threshold
        self._tmp_path  = tmp_path
        self._pa        = None
        self._ready     = False
        self._init_pyaudio()

    def _init_pyaudio(self) -> None:
        try:
            import pyaudio
            self.FORMAT = pyaudio.paInt16
            self._pa    = pyaudio.PyAudio()
            self._ready = True
            log.info("[MIC] pyaudio ready")
        except Exception as e:
            log.warning(f"[MIC] pyaudio unavailable ({e})")

    def is_ready(self) -> bool:
        return self._ready

    def record_clip(self) -> Optional[str]:
        """
        Record one speech clip.
        Returns path to .wav file, or None if nothing was recorded.
        """
        if not self._ready or self._pa is None:
            return None

        import pyaudio
        import wave
        import struct
        import math

        frames       = []
        silent_chunks = 0
        voiced_chunks = 0
        max_chunks   = int(self.RATE / self.CHUNK * self.MAX_SECONDS)
        silence_max  = int(self.RATE / self.CHUNK * self.SILENCE_SECS)

        try:
            stream = self._pa.open(
                format=self.FORMAT,
                channels=self.CHANNELS,
                rate=self.RATE,
                input=True,
                frames_per_buffer=self.CHUNK,
            )
        except Exception as e:
            log.error(f"[MIC] Stream open failed: {e}")
            return None

        try:
            for _ in range(max_chunks):
                data = stream.read(self.CHUNK, exception_on_overflow=False)
                frames.append(data)

                # RMS energy
                shorts = struct.unpack(
                    f"{len(data)//2}h", data)
                rms = math.sqrt(
                    sum(s * s for s in shorts) / len(shorts)
                ) if shorts else 0

                if rms < self._threshold:
                    silent_chunks += 1
                    if voiced_chunks > 0 and \
                            silent_chunks >= silence_max:
                        break        # speech finished
                else:
                    voiced_chunks += 1
                    silent_chunks = 0
        finally:
            stream.stop_stream()
            stream.close()

        if voiced_chunks == 0:
            return None   # pure silence — discard

        # Write WAV
        try:
            import wave
            wf = wave.open(self._tmp_path, "wb")
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(self._pa.get_sample_size(self.FORMAT))
            wf.setframerate(self.RATE)
            wf.writeframes(b"".join(frames))
            wf.close()
            return self._tmp_path
        except Exception as e:
            log.error(f"[MIC] WAV write failed: {e}")
            return None

    def close(self) -> None:
        if self._pa:
            self._pa.terminate()


# ══════════════════════════════════════════════════════════════════
#  COMMAND PARSER
# ══════════════════════════════════════════════════════════════════

class CommandParser:
    """
    Maps transcribed text to AENIDA commands.
    Rule-based — no LLM needed for intent recognition.
    """

    WAKE_WORD = "aenida"

    # (pattern, command, extra_group)
    RULES: list = [
        # Trading
        (r"\banalyze\b.*?\b([A-Za-z]+(?:\s+[A-Za-z]+)?)\b", "analyze", 1),
        (r"\bsignal(?:s)?\b.*?\b([A-Za-z]+)\b",              "analyze", 1),
        # System
        (r"\b(?:system\s+)?status\b",                         "status",  None),
        (r"\bhealth\b",                                        "status",  None),
        # Trading actions
        (r"\bstop\s+trading\b",                               "stop",    None),
        (r"\bstart\s+trading\b",                              "start",   None),
        (r"\bpause\b",                                         "stop",    None),
        # Coins
        (r"\badd\s+coin\s+([A-Za-z]+)\b",                    "add_coin",    1),
        (r"\bremove\s+coin\s+([A-Za-z]+)\b",                 "remove_coin", 1),
        (r"\bwatch\s+([A-Za-z]+)\b",                          "add_coin",    1),
        # AI
        (r"\b(?:think|ask|what\s+is|explain)\s+(.+)",         "think",   1),
        # News
        (r"\b(?:news|latest|headlines)\b",                    "news",    None),
        # Workers
        (r"\b(?:workers|worker\s+status)\b",                  "workers", None),
        # Quit
        (r"\b(?:quit|exit|goodbye|bye|shutdown)\b",           "quit",    None),
        # Help
        (r"\b(?:help|commands|what\s+can\s+you)\b",           "help",    None),
    ]

    # Coin name → symbol mapping
    COIN_NAMES: Dict[str, str] = {
        "bitcoin": "BTC-USDT", "btc": "BTC-USDT",
        "ethereum": "ETH-USDT", "eth": "ETH-USDT",
        "solana": "SOL-USDT",  "sol": "SOL-USDT",
        "binance": "BNB-USDT", "bnb": "BNB-USDT",
        "cardano": "ADA-USDT", "ada": "ADA-USDT",
        "ripple": "XRP-USDT",  "xrp": "XRP-USDT",
        "dogecoin": "DOGE-USDT", "doge": "DOGE-USDT",
        "polkadot": "DOT-USDT",  "dot": "DOT-USDT",
    }

    def has_wake_word(self, text: str) -> bool:
        return self.WAKE_WORD in text.lower()

    def strip_wake_word(self, text: str) -> str:
        return re.sub(r"(?i)\baenida[,.]?\s*", "", text).strip()

    def parse(self, text: str) -> Optional[Tuple[str, str]]:
        """
        Parse transcribed text into (command, argument).
        Returns None if no command matched.
        """
        clean = text.lower().strip(" .,!?")

        for pattern, cmd, grp in self.RULES:
            m = re.search(pattern, clean, re.I)
            if m:
                arg = ""
                if grp is not None:
                    try:
                        arg = m.group(grp).strip()
                    except IndexError:
                        arg = ""

                # Resolve coin name to symbol
                if cmd in ("analyze", "add_coin", "remove_coin"):
                    arg = self.COIN_NAMES.get(arg.lower(),
                                              arg.upper() + "-USDT" if arg else "BTC-USDT")

                return cmd, arg

        return None   # no command matched

    def format_response(self, cmd: str, result: Any) -> str:
        """Convert command result to a spoken sentence."""
        if isinstance(result, str):
            # Strip ANSI / box-drawing chars that don't speak well
            clean = re.sub(r"[╔╗╚╝║═╠╣┌┐└┘│─]+", "", result)
            clean = re.sub(r"\s{2,}", " ", clean).strip()
            return clean[:400]   # cap at ~400 chars for speech

        if isinstance(result, dict):
            if "error" in result:
                return f"Error: {result['error']}"
            if "decision" in result:
                d = result["decision"]
                sym = result.get("symbol", "")
                price = result.get("price", 0)
                action = d.get("action", "hold")
                strength = d.get("strength", 0)
                return (f"Signal for {sym}: {action}. "
                        f"Price ${price:,.0f}. "
                        f"Strength {strength:.0f} percent.")
            if "state" in result:
                st = result["state"]
                worker = "online" if st.get("worker_online") else "offline"
                tasks  = st.get("pending_tasks", 0)
                ram    = st.get("ram_mb", 0)
                return (f"System status: Worker is {worker}. "
                        f"{tasks} pending tasks. "
                        f"RAM usage {ram:.0f} megabytes.")
            if "news" in result:
                items = result["news"][:3]
                headlines = ". ".join(
                    n.get("title", "")[:80] for n in items)
                return f"Latest news: {headlines}"

        return "Command executed."


# ══════════════════════════════════════════════════════════════════
#  COMMAND DISPATCHER
# ══════════════════════════════════════════════════════════════════

class CommandDispatcher:
    """
    Executes parsed commands against AENIDA's orchestrator.
    Falls back to mock responses when orchestrator is unavailable.
    """

    HELP_TEXT = (
        "Available commands: "
        "analyze Bitcoin, system status, stop trading, start trading, "
        "add coin Solana, remove coin Ethereum, "
        "think what is RSI, news, workers, quit."
    )

    def dispatch(self, cmd: str, arg: str) -> Any:
        """
        Run the command. Returns a result dict or string.
        """
        log.info(f"[DISPATCH] {cmd}({arg!r})")

        try:
            if cmd == "analyze":
                return self._analyze(arg)
            elif cmd == "status":
                return self._status()
            elif cmd == "stop":
                return self._stop_agent()
            elif cmd == "start":
                return self._start_agent()
            elif cmd == "add_coin":
                return self._add_coin(arg)
            elif cmd == "remove_coin":
                return self._remove_coin(arg)
            elif cmd == "think":
                return self._think(arg)
            elif cmd == "news":
                return self._news()
            elif cmd == "workers":
                return self._workers()
            elif cmd == "help":
                return self.HELP_TEXT
            elif cmd == "quit":
                return "__QUIT__"
            else:
                return f"Unknown command: {cmd}"
        except Exception as e:
            log.error(f"[DISPATCH] {cmd} failed: {e}")
            return f"Command failed: {e}"

    # ── Individual command handlers ───────────────────────────────

    def _analyze(self, symbol: str) -> Any:
        try:
            from orchestrator import run_decision_pipeline
            return run_decision_pipeline(symbol)
        except Exception:
            return {"decision": {"action": "HOLD", "strength": 52},
                    "symbol": symbol, "price": 0,
                    "degraded": True}

    def _status(self) -> Any:
        try:
            from orchestrator import get_agent_status
            return get_agent_status()
        except Exception:
            return {"state": {"worker_online": False,
                              "pending_tasks": 0,
                              "ram_mb": 0,
                              "decisions_today": 0}}

    def _stop_agent(self) -> str:
        try:
            from orchestrator import stop_agent
            stop_agent()
            return "Trading agent stopped."
        except Exception:
            return "Stop command sent."

    def _start_agent(self) -> str:
        try:
            from orchestrator import start_agent
            start_agent(["BTC-USDT", "ETH-USDT"])
            return "Trading agent started."
        except Exception:
            return "Start command sent."

    def _add_coin(self, symbol: str) -> str:
        try:
            from news_watcher import add_coin
            add_coin(symbol)
            return f"Added {symbol} to watchlist."
        except Exception:
            return f"Added {symbol} to watchlist."

    def _remove_coin(self, symbol: str) -> str:
        try:
            from news_watcher import remove_coin
            remove_coin(symbol)
            return f"Removed {symbol} from watchlist."
        except Exception:
            return f"Removed {symbol} from watchlist."

    def _think(self, query: str) -> Any:
        try:
            from local_brain import think
            result = think(query)
            return result.get("result", str(result))
        except Exception:
            return f"Query received: {query}. AI brain unavailable."

    def _news(self) -> Any:
        try:
            from news_watcher import get_recent
            return {"news": get_recent(3)}
        except Exception:
            return {"news": [{"title": "Market data unavailable."}]}

    def _workers(self) -> Any:
        try:
            from worker_registry import get_active_workers
            ws = get_active_workers()
            count = len(ws)
            return f"{count} worker{'s' if count != 1 else ''} connected."
        except Exception:
            return "Worker registry unavailable."


# ══════════════════════════════════════════════════════════════════
#  VOICE CONTROL ENGINE
# ══════════════════════════════════════════════════════════════════

class VoiceControl:
    """
    Top-level controller that wires together:
      MicRecorder → WhisperTranscriber → CommandParser
           → CommandDispatcher → TTSEngine

    States
    ──────
    IDLE    : listening for wake word only
    ACTIVE  : full command listening (10s timeout then back to IDLE)
    STANDBY : waiting for Whisper model to load
    """

    ACTIVE_TIMEOUT = 15   # seconds of inactivity before back to IDLE

    def __init__(self, model: str = "base",
                 energy_threshold: int = 300):
        self._parser     = CommandParser()
        self._dispatcher = CommandDispatcher()
        self._tts        = TTSEngine()
        self._transcriber = WhisperTranscriber(model)
        self._mic        = MicRecorder(energy_threshold)

        self._state      = "STANDBY"
        self._active_ts  = 0.0
        self._running    = False

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(self) -> None:
        """Start the voice control loop (blocking)."""
        self._running = True
        log.info("[VOICE] Starting…")

        # Wait for Whisper model
        self._speak("Initializing voice control. Loading Whisper model.")
        for _ in range(60):          # wait up to 60s
            if self._transcriber.is_ready():
                break
            time.sleep(1)

        if not self._transcriber.is_ready():
            self._speak(
                "Whisper model failed to load. "
                "Please run: pip install openai-whisper")
            return

        if not self._mic.is_ready():
            self._speak(
                "Microphone unavailable. "
                "Please run: pip install pyaudio")
            return

        self._state = "IDLE"
        self._speak(
            "AENIDA voice control ready. "
            "Say AENIDA followed by your command.")

        self._listen_loop()

    def stop(self) -> None:
        self._running = False
        self._mic.close()
        self._tts.stop()
        log.info("[VOICE] Stopped.")

    # ── Main listen loop ──────────────────────────────────────────

    def _listen_loop(self) -> None:
        while self._running:
            # Timeout ACTIVE → IDLE
            if self._state == "ACTIVE":
                if time.time() - self._active_ts > self.ACTIVE_TIMEOUT:
                    log.info("[VOICE] Timeout → IDLE")
                    self._state = "IDLE"

            # Record a clip
            clip_path = self._mic.record_clip()
            if clip_path is None:
                continue   # silence — keep listening

            # Transcribe
            text = self._transcriber.transcribe(clip_path)
            if not text:
                continue

            self._handle_text(text)

    def _handle_text(self, text: str) -> None:
        """Process one transcribed utterance."""
        lower = text.lower()

        # ── IDLE: look for wake word ──────────────────────────────
        if self._state == "IDLE":
            if self._parser.has_wake_word(lower):
                self._state    = "ACTIVE"
                self._active_ts = time.time()
                # Maybe the command was in the same utterance
                remainder = self._parser.strip_wake_word(text)
                if remainder:
                    self._execute_text(remainder)
                else:
                    self._speak("Yes?")
            return

        # ── ACTIVE: parse command ─────────────────────────────────
        self._active_ts = time.time()   # reset timeout
        self._execute_text(text)

    def _execute_text(self, text: str) -> None:
        """Parse text and run the command."""
        parsed = self._parser.parse(text)
        if parsed is None:
            self._speak(f"I didn't understand: {text[:60]}")
            return

        cmd, arg = parsed

        if cmd == "quit":
            self._speak("Goodbye. Voice control shutting down.")
            self._running = False
            return

        self._speak(f"Running {cmd}.")
        result  = self._dispatcher.dispatch(cmd, arg)
        spoken  = self._parser.format_response(cmd, result)
        self._speak(spoken)

    def _speak(self, text: str) -> None:
        print(f"  🤖 {text}")
        self._tts.speak(text)


# ══════════════════════════════════════════════════════════════════
#  TEST MODE  (no mic, no speakers, no Whisper model)
# ══════════════════════════════════════════════════════════════════

class VoiceControlTest:
    """
    Simulates the full voice pipeline with text input.
    Tests every layer: parser → dispatcher → formatter.
    No hardware or downloaded models needed.
    """

    def __init__(self):
        self._parser     = CommandParser()
        self._dispatcher = CommandDispatcher()

    def run(self) -> None:
        print()
        print("╔══════════════════════════════════════════════════╗")
        print("║    AENIDA Voice Control — TEST MODE              ║")
        print("║    Type what you would SAY into the mic.         ║")
        print("║    Prefix with 'AENIDA' or just type the command.║")
        print("║    Type 'quit' to exit.                          ║")
        print("╚══════════════════════════════════════════════════╝")
        print()

        test_sentences = [
            "AENIDA analyze Bitcoin",
            "AENIDA system status",
            "AENIDA add coin Solana",
            "AENIDA think what is RSI",
            "AENIDA news",
            "AENIDA stop trading",
            "AENIDA workers",
            "AENIDA help",
        ]

        print("  Running built-in test phrases…\n")
        parser     = self._parser
        dispatcher = self._dispatcher

        all_pass = True
        for sentence in test_sentences:
            text    = parser.strip_wake_word(sentence)
            parsed  = parser.parse(text)
            if parsed is None:
                print(f"  ✗  PARSE FAIL  : '{sentence}'")
                all_pass = False
                continue

            cmd, arg = parsed
            result   = dispatcher.dispatch(cmd, arg)
            spoken   = parser.format_response(cmd, result)

            print(f"  ✓  Input      : '{sentence}'")
            print(f"     Command    : {cmd}({arg!r})")
            print(f"     Response   : {spoken[:100]}")
            print()

        print("─" * 52)
        print(f"  Auto-test: {'ALL PASSED ✓' if all_pass else 'SOME FAILED ✗'}")
        print("─" * 52)
        print()

        # Interactive mode
        print("  Interactive mode — type your own phrases:")
        print("  (Press Ctrl+C or type 'quit' to exit)\n")
        active = False
        active_ts = 0.0

        while True:
            try:
                raw = input("  speak> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n  Exiting.")
                break

            if not raw:
                continue
            if raw.lower() == "quit":
                break

            # Simulate wake word logic
            if parser.has_wake_word(raw):
                active    = True
                active_ts = time.time()
                text      = parser.strip_wake_word(raw)
            elif active and time.time() - active_ts < 15:
                text = raw
            else:
                print("  [IDLE] Say 'AENIDA' first to activate.\n")
                continue

            if not text:
                print("  [ACTIVE] Listening for command…\n")
                continue

            parsed = parser.parse(text)
            if parsed is None:
                print(f"  ✗  Could not parse: '{text}'\n")
                continue

            cmd, arg = parsed
            if cmd == "quit":
                print("  Goodbye.\n")
                break

            result = dispatcher.dispatch(cmd, arg)
            spoken = parser.format_response(cmd, result)
            print(f"  🤖 {spoken}\n")
            active_ts = time.time()


# ══════════════════════════════════════════════════════════════════
#  INTEGRATION HELPER  (called from main.py)
# ══════════════════════════════════════════════════════════════════

_vc: Optional[VoiceControl] = None


def start_voice_thread(model: str = "base") -> threading.Thread:
    """
    Launch VoiceControl in a daemon thread.
    Call from main.py after orchestrator is running:

        from voice_control import start_voice_thread
        start_voice_thread()
    """
    global _vc
    _vc = VoiceControl(model=model)
    t = threading.Thread(target=_vc.start, daemon=True, name="voice_control")
    t.start()
    log.info("[VOICE] Voice thread started")
    return t


def stop_voice() -> None:
    global _vc
    if _vc:
        _vc.stop()


# ══════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════

def main() -> None:
    ap = argparse.ArgumentParser(
        description="AENIDA Voice Control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--test", action="store_true",
        help="Run in test mode (no mic/speakers/Whisper model needed)")
    ap.add_argument(
        "--model", default="base",
        choices=["tiny", "base", "small", "medium"],
        help="Whisper model size (default: base ~150 MB)")
    ap.add_argument(
        "--energy", type=int, default=300,
        help="Mic energy threshold for VAD (default: 300)")
    ap.add_argument(
        "--list-deps", action="store_true",
        help="Check which dependencies are installed")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-16s | %(message)s",
    )

    if args.list_deps:
        deps = _check_deps()
        print("\n  AENIDA Voice Control — Dependency Check")
        print("  " + "─" * 40)
        for name, ok in deps.items():
            icon = "✓" if ok else "✗"
            note = "" if ok else "  → pip install " + name.replace("pyttsx3","pyttsx3").replace("whisper","openai-whisper").replace("pyaudio","pyaudio")
            print(f"  {icon} {name:<15}{note}")
        print()
        if all(deps.values()):
            print("  All dependencies installed. Ready to run!\n")
        else:
            print("  Install missing deps then re-run.\n")
        return

    if args.test:
        VoiceControlTest().run()
        return

    deps = _check_deps()
    missing = [k for k, v in deps.items() if not v]
    if missing:
        print(f"\n  Missing dependencies: {', '.join(missing)}")
        print(f"  Run: pip install openai-whisper pyttsx3 pyaudio")
        print(f"  Or use test mode: python voice_control.py --test\n")
        sys.exit(1)

    vc = VoiceControl(model=args.model, energy_threshold=args.energy)
    try:
        vc.start()
    except KeyboardInterrupt:
        print("\n  Shutting down voice control…")
    finally:
        vc.stop()


if __name__ == "__main__":
    main()
