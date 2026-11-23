"""Languages a business's callers can switch to (besides English).

A business picks languages in Settings (settings.languages, e.g. ["te-IN"]; [] for none).
Until it chooses, it offers DEFAULT_LANGUAGES (Telugu, Hindi, Tamil and Kannada). For such a
business, /voice declares each language to ConversationRelay (its voice and speech
recognition) and the greeting asks "Would you like to continue in English, or in Telugu,
Hindi, Tamil or Kannada?". The caller switches by answering or asking (the agent's
switch_language tool); the keypad keys (2 Telugu, 3 Hindi, 4 Tamil, 5 Kannada) still work
but aren't announced. Before the agent's next words, the server sends ConversationRelay a
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

ENGLISH = "en-US"  # the call's language code for English (session.language, switch_language)
ENGLISH_SPEECH = "en-IN"  # the English voice speaks Indian English by default


def english_speech() -> str:
    """The English voice's language: Indian English by default (TTS_LANGUAGE)."""
    value = os.environ.get("TTS_LANGUAGE", "").strip()
    return ENGLISH if value.lower() == "none" else (value or ENGLISH_SPEECH)



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
    stt_code: str = ""  # recognition's code when it differs (Deepgram names Telugu "te")
    # Recognition set only in the switch message, not in the call setup:
    # "provider:model:code", e.g. "Deepgram:nova-3-general:te" (TELUGU_SWITCH_STT).
    switch_stt: str = ""

    @property
    def listen_code(self) -> str:
        return self.stt_code or self.code

    def noun(self) -> str:
        """The <Language> element(s) declaring it to ConversationRelay: one for both voice
        and recognition, or, when recognition uses another code, one for each."""
        def el(attrs):
            return "<Language " + " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items() if v) + " />"
        tts = {"ttsProvider": self.tts_provider, "voice": self.voice}
        stt = {"transcriptionProvider": self.transcription_provider, "speechModel": self.speech_model}
        if self.listen_code == self.code:
            return el({"code": self.code, **tts, **stt})
        return el({"code": self.code, **tts}) + el({"code": self.listen_code, **stt})

    def switch_message(self) -> dict:
        if self.switch_stt.count(":") == 2:
            provider, model, code = self.switch_stt.split(":")
            # A language the session wasn't set up with: give the recognition part in full.
            return {"type": "language", "ttsLanguage": self.code, "transcriptionLanguage": code,
                    "transcriptionProvider": provider, "speechModel": model}
        return {"type": "language", "ttsLanguage": self.code, "transcriptionLanguage": self.listen_code}


def _indian(code: str, label: str, native: str, key: str, env: str, voice: str) -> Language:
    """An Indian language, set up the way Telugu was proven on real calls: an ElevenLabs
    v4_turbo voice (the real-time ElevenLabs model for Indian languages), Google `long`
    recognition declared (Twilio accepts it in the call setup), and Deepgram nova-3
    multilingual listening set in the switch message. Every part can be overridden by env
    (<ENV>_VOICE, <ENV>_TTS_PROVIDER, <ENV>_STT_PROVIDER, <ENV>_SPEECH_MODEL, <ENV>_STT_CODE,
    <ENV>_SWITCH_STT) without a code change."""
    e = lambda name, default: os.environ.get(f"{env}_{name}", default)
    return Language(
        code=code, label=label, native=native, key=key,
        tts_provider=e("TTS_PROVIDER", "ElevenLabs"),
        voice=e("VOICE", voice),
        transcription_provider=e("STT_PROVIDER", "Google"),
        # Twilio uses Google's global endpoint, where Indian languages have `long` and `short`
        # (chirp_3 is EU-only for Telugu; Twilio rejected it with error 64101).
        speech_model=e("SPEECH_MODEL", "long"),
        stt_code=e("STT_CODE", ""),
        # Listening: Google (declared above, accepted by Twilio) never delivered usable text
        # on real Telugu calls. Deepgram nova-3 in multilingual mode ("multi"), set in the
        # switch message, does, fast and in the language's own script. Twilio rejects Deepgram
        # with "te" (64107) or "te-IN" (64114), and in the call setup (64101). "none" uses the
        # declared Google recognition.
        switch_stt=e("SWITCH_STT", "Deepgram:nova-3-general:multi").strip().replace("none", ""),
        confirm=f"(The caller pressed {key}. The call is already switched to {label}, so don't call "
                f"switch_language. Greet them in one short {label} sentence and ask what they'd like.)",
    )


# The Indian English voice (server.VOICE_DEFAULTS) on v4_turbo, for Hindi, Tamil and Kannada.
# Telugu keeps the voice it was tested and approved with.
_INDIAN_VOICE = "mCQMfsqGDT6IDkEKR20a-v4_turbo"


def available() -> dict[str, Language]:
    """Languages businesses can offer, by code, in the order the greeting names them (read
    per call so env overrides apply). Keypad keys still switch, though the greeting asks
    instead of offering keys."""
    langs = [
        _indian("te-IN", "Telugu", "తెలుగు", "2", "TELUGU", "UgBBYS2sOqTuMpoF3BR0-v4_turbo"),
        _indian("hi-IN", "Hindi", "हिन्दी", "3", "HINDI", _INDIAN_VOICE),
        _indian("ta-IN", "Tamil", "தமிழ்", "4", "TAMIL", _INDIAN_VOICE),
        _indian("kn-IN", "Kannada", "ಕನ್ನಡ", "5", "KANNADA", _INDIAN_VOICE),
    ]
    return {lang.code: lang for lang in langs}


def default_codes() -> list[str]:
    """Languages a business offers until it chooses in Settings: DEFAULT_LANGUAGES
    (comma-separated, default all of them; empty turns the default off for everyone)."""
    raw = os.environ.get("DEFAULT_LANGUAGES", ",".join(available()))
    return [c.strip() for c in raw.split(",") if c.strip()]


def codes_for(settings: dict) -> list[str]:
    """The business's choice (settings.languages, even an empty one), else the default."""
    chosen = settings.get("languages")
    return list(chosen) if isinstance(chosen, list) else default_codes()


def for_business(settings: dict) -> list[Language]:
    """The business's extra languages, in its order (unknown codes skipped)."""
    langs = available()
    return [langs[c] for c in codes_for(settings) if c in langs]


def names(languages: list[Language]) -> str:
    """"Telugu, Hindi, Tamil or Kannada"."""
    labels = [lang.label for lang in languages]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " or " + labels[-1]


def greeting_question(languages: list[Language]) -> str:
    """Asked as part of the greeting, in English, instead of an IVR-style keypad offer."""
    return f"Would you like to continue in English, or in {names(languages)}?"


def by_key(languages: list[Language], digit: str) -> Language | None:
    return next((lang for lang in languages if lang.key == str(digit).strip()), None)


def english_switch_message() -> dict:
    """Back to English: the English voice (Indian English by default), English listening."""
    return {"type": "language", "ttsLanguage": english_speech(), "transcriptionLanguage": ENGLISH}
