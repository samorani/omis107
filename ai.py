"""Voice-to-draft-events pipeline, kept behind two functions.

The rest of the app only ever calls transcribe() and parse_transcript(), so
swapping vendors -- or moving to a model that takes audio directly and collapses
the two steps into one -- is a change to this file alone.

Provider is chosen by the AI_PROVIDER environment variable. When it is unset we
use OpenAI if an API key is present and the stub otherwise, so the app runs
end to end before anyone has signed up for anything.
"""
import os
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel

# Kept in sync with EVENT_CATEGORIES in app.py.
CATEGORIES = ['Subcontractor', 'Inspection', 'Delivery', 'Client', 'Payment', 'Other']

# gpt-transcribe is the current general-purpose transcription model, and the
# cheapest of the accurate ones (~$0.0045/minute).
TRANSCRIBE_MODEL = os.environ.get('OPENAI_TRANSCRIBE_MODEL', 'gpt-transcribe')
PARSE_MODEL = os.environ.get('OPENAI_PARSE_MODEL', 'gpt-5.6')

# OpenAI accepts mp3, mp4, mpeg, mpga, m4a, wav and webm, up to 25 MB.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class ParsedEvent(BaseModel):
    """One reminder the model heard in the memo.

    Every field is required but nullable, which is what strict structured
    outputs wants -- an omitted field is an error, an unknown one is null.
    """
    title: str
    event_date: str                      # YYYY-MM-DD
    event_time: Optional[str]            # HH:MM in 24h, or null if none was said
    category: Literal['Subcontractor', 'Inspection', 'Delivery', 'Client', 'Payment', 'Other']
    notes: Optional[str]


class ParsedEvents(BaseModel):
    events: list[ParsedEvent]


SYSTEM_PROMPT = """\
You turn a contractor's spoken memo into calendar reminders for one remodeling job.

Today is {today} ({weekday}). Resolve every relative day against that date:
"Monday" and "next Monday" both mean the NEXT Monday that has not happened yet.

Rules:
- One memo often contains SEVERAL reminders. "Electrician's coming Monday and
  the gas gets turned back on Tuesday" is TWO separate events. Return one entry
  per thing that happens on its own day or at its own time.
- title: short, like a note to self. "Electrician rough-in", not a sentence.
- event_time: only when a clock time was actually said. Null otherwise -- do not
  invent 9am for "Tuesday".
- category: Subcontractor (trades on site), Inspection (inspectors, permits,
  sign-offs), Delivery (materials, equipment, utility hookups), Client (the
  homeowner), Payment (invoices, draws), Other.
- notes: phone numbers, names, and anything else said that does not fit the
  title. Null if there is nothing extra.
- Transcription is imperfect. If a word is garbled, keep your best guess in the
  title rather than dropping the whole reminder -- a person reviews every one of
  these before it counts.
- If the memo contains nothing schedulable, return an empty list.

This job is "{project_name}"."""


def provider_name():
    """Which backend is live. Shown in the UI so a stub is never mistaken for real."""
    configured = os.environ.get('AI_PROVIDER')
    if configured:
        return configured
    return 'openai' if os.environ.get('OPENAI_API_KEY') else 'stub'


def _client():
    from openai import OpenAI
    return OpenAI()


# --------------------------------------------------------------------------
# Transcription: audio file -> text
# --------------------------------------------------------------------------

def transcribe(audio_path):
    if provider_name() == 'stub':
        return _stub_transcribe()

    with open(audio_path, 'rb') as audio_file:
        result = _client().audio.transcriptions.create(
            model=TRANSCRIBE_MODEL,
            file=audio_file,
        )
    return (result.text or '').strip()


def _stub_transcribe():
    return ("Electrician is coming Monday morning at eight for the rough-in, "
            "Mike's number is 408-555-0199. And the gas gets reattached Tuesday.")


# --------------------------------------------------------------------------
# Parsing: text -> list of draft events
# --------------------------------------------------------------------------

def parse_transcript(transcript, project_name, today=None):
    """Return a list of plain dicts, one per reminder heard.

    Nothing here is trusted: app.py validates every date and category before
    anything reaches the database, and a person confirms it after that.
    """
    today = today or date.today()

    if provider_name() == 'stub':
        return _stub_parse(today)

    weekday = ['Monday', 'Tuesday', 'Wednesday', 'Thursday',
               'Friday', 'Saturday', 'Sunday'][today.weekday()]

    response = _client().responses.parse(
        model=PARSE_MODEL,
        input=[
            {'role': 'system', 'content': SYSTEM_PROMPT.format(
                today=today.isoformat(), weekday=weekday, project_name=project_name)},
            {'role': 'user', 'content': transcript},
        ],
        text_format=ParsedEvents,
    )

    parsed = response.output_parsed
    if parsed is None:          # refusal, or the model returned nothing usable
        return []
    return [event.model_dump() for event in parsed.events]


def _stub_parse(today):
    """Two events from one memo, so the multi-draft path is exercised offline."""
    from datetime import timedelta
    monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
    return [
        {'title': 'Electrician rough-in', 'event_date': monday.isoformat(),
         'event_time': '08:00', 'category': 'Subcontractor',
         'notes': 'Mike, 408-555-0199'},
        {'title': 'Gas reattached', 'event_date': (monday + timedelta(days=1)).isoformat(),
         'event_time': None, 'category': 'Delivery', 'notes': None},
    ]
