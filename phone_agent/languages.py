"""Languages a business's callers can switch to (besides English).

A business picks languages in Settings (settings.languages, e.g. ["te-IN"]; [] for none).
Until it chooses, it offers DEFAULT_LANGUAGES (Telugu for now). For such a
business, /voice declares each language to ConversationRelay (its voice and speech
recognition), turns on keypad detection, and the greeting ends with "For Telugu, press 2".
The caller switches by pressing the key or by asking; the agent's switch_language tool
does the same. Before the agent's next words, the server sends ConversationRelay a
"language" message so both its voice and its listening change (server.RelayCall).

There's no automatic detection: Twilio only detects languages with Deepgram and
ElevenLabs, which don't cover Telugu. An invalid voice/model combination stops a call
from connecting at all (Twilio error 64101), so each language's settings can be
overridden by env without a code change, and only businesses that turn a language on
are affected.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from xml.sax.saxutils import quoteattr

ENGLISH = "en-US"


@dataclass(frozen=True)
class Language:
    code: str  # BCP-47, as ConversationRelay wants it
    label: str  # in English
    native: str  # in its own script
    key: str  # keypad digit that switches to it
    tts_provider: str
    voice: str
    transcription_provider: str
    speech_model: str
    confirm: str  # what the agent hears when the caller presses the key

    def noun(self) -> str:
        """The <Language> element declaring it to ConversationRelay."""
        attrs = {"code": self.code, "ttsProvider": self.tts_provider, "voice": self.voice,
                 "transcriptionProvider": self.transcription_provider, "speechModel": self.speech_model}
        return "<Language " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items() if v) + " />"

    def switch_message(self) -> dict:
        return {"type": "language", "ttsLanguage": self.code, "transcriptionLanguage": self.code}


def _telugu() -> Language:
    return Language(
        code="te-IN", label="Telugu", native="తెలుగు", key="2",
        tts_provider=os.environ.get("TELUGU_TTS_PROVIDER", "Google"),
        voice=os.environ.get("TELUGU_VOICE", "te-IN-Chirp3-HD-Kore"),
        transcription_provider=os.environ.get("TELUGU_STT_PROVIDER", "Google"),
        # Twilio uses Google's global endpoint, where te-IN has only `long` and `short`
        # (chirp_3 is EU-only for Telugu; Twilio rejected it with error 64101).
        speech_model=os.environ.get("TELUGU_SPEECH_MODEL", "long"),
        confirm="(The caller pressed 2. The call is already switched to Telugu, so don't call "
                "switch_language. Greet them in one short Telugu sentence and ask what they'd like.)",
    )


def available() -> dict[str, Language]:
    """Languages businesses can offer, by code (read per call so env overrides apply)."""
    lang = _telugu()
    return {lang.code: lang}


def default_codes() -> list[str]:
    """Languages a business offers until it chooses in Settings: DEFAULT_LANGUAGES
    (comma-separated, default "te-IN"; empty turns the default off for everyone)."""
    raw = os.environ.get("DEFAULT_LANGUAGES", "te-IN")
    return [c.strip() for c in raw.split(",") if c.strip()]


def codes_for(settings: dict) -> list[str]:
    """The business's choice (settings.languages, even an empty one), else the default."""
    chosen = settings.get("languages")
    return list(chosen) if isinstance(chosen, list) else default_codes()


def for_business(settings: dict) -> list[Language]:
    """The business's extra languages, in its order (unknown codes skipped)."""
    langs = available()
    return [langs[c] for c in codes_for(settings) if c in langs]


def greeting_offer(languages: list[Language]) -> str:
    """Said after the greeting, in English so every caller understands the keypad offer."""
    return " ".join(f"For {lang.label}, press {lang.key}." for lang in languages)


def by_key(languages: list[Language], digit: str) -> Language | None:
    return next((lang for lang in languages if lang.key == str(digit).strip()), None)


def english_switch_message() -> dict:
    return {"type": "language", "ttsLanguage": ENGLISH, "transcriptionLanguage": ENGLISH}
