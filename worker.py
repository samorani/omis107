"""Background processing for voice memos.

The upload route only writes a row and returns. Everything slow -- transcription
and parsing -- happens here, on a thread, so the contractor can record the next
memo straight away or close the browser and find the drafts waiting later.

The queue is the voice_notes table itself. That matters: once the audio is on
disk and the row is committed, the work survives a browser closing, a lost
connection, or the web process being restarted mid-job.
"""
import logging
import os
import threading
import time

import psycopg
from psycopg.rows import dict_row

import ai

log = logging.getLogger(__name__)

POLL_SECONDS = 3
# A job still 'processing' after this long means the process handling it died.
STALE_MINUTES = 10
MAX_ATTEMPTS = 3

_started = threading.Lock()
_running = False


def _connect(database_url):
    """A connection of our own -- Flask's `g` is request-scoped and thread-local."""
    return psycopg.connect(database_url, row_factory=dict_row)


def claim_next(db):
    """Take one pending job, atomically.

    FOR UPDATE SKIP LOCKED lets several gunicorn workers poll the same queue
    without ever handing the same recording to two of them.
    """
    row = db.execute(
        """UPDATE voice_notes
              SET status = 'processing', started_at = now(), attempts = attempts + 1
            WHERE id = (SELECT v.id
                          FROM voice_notes v
                         WHERE v.status = 'pending'
                         ORDER BY v.id
                         LIMIT 1
                         FOR UPDATE SKIP LOCKED)
        RETURNING *""").fetchone()
    db.commit()
    return row


def requeue_stale(db):
    """Return jobs abandoned by a dead process, unless they have failed too often."""
    # Build the interval by multiplication rather than interpolating into a
    # quoted literal -- placeholders inside SQL strings are a trap.
    n = db.execute(
        """UPDATE voice_notes
              SET status = CASE WHEN attempts >= %(max)s THEN 'failed' ELSE 'pending' END,
                  error = CASE WHEN attempts >= %(max)s
                               THEN 'Gave up after repeated attempts.' ELSE error END
            WHERE status = 'processing'
              AND started_at < now() - (%(stale)s * interval '1 minute')""",
        {'max': MAX_ATTEMPTS, 'stale': STALE_MINUTES}).rowcount
    db.commit()
    if n:
        log.warning('requeued %d stale voice note(s)', n)


def process(db, note, coerce, upload_dir):
    """Transcribe, parse, and write drafts for one recording."""
    project_name = None
    if note['project_id'] is not None:
        project = db.execute('SELECT name FROM projects WHERE id = %s',
                             (note['project_id'],)).fetchone()
        if project is None:                  # job deleted while queued
            db.execute("UPDATE voice_notes SET status = 'failed', error = %s, "
                       'processed_at = now() WHERE id = %s',
                       ('The job was deleted.', note['id']))
            db.commit()
            return
        project_name = project['name']

    # Memos recorded from the dashboard have no job, so offer the model the
    # names it could be talking about.
    jobs = db.execute('SELECT id, name FROM projects WHERE user_id = %s ORDER BY name',
                      (note['user_id'],)).fetchall()
    by_name = {j['name'].strip().lower(): j['id'] for j in jobs}

    audio_path = os.path.join(upload_dir, note['audio_filename'])
    transcript = ai.transcribe(audio_path)
    if not transcript:
        db.execute("UPDATE voice_notes SET status = 'failed', processed_at = now(), error = %s "
                   'WHERE id = %s',
                   ('That recording came back empty -- try again, closer to the mic.', note['id']))
        db.commit()
        return

    candidates = ai.parse_transcript(transcript, project_name=project_name,
                                     job_names=[j['name'] for j in jobs])

    created = 0
    for candidate in candidates:
        values = coerce(candidate)
        if values is None:                   # unusable date -- drop rather than guess
            continue
        # The memo's own job wins; otherwise take the one the model named, but
        # only on an exact (case-insensitive) match against a job that exists.
        # A near-miss becomes null, which just asks the person to pick.
        target = note['project_id']
        if target is None:
            heard = (candidate.get('job') or '').strip().lower()
            target = by_name.get(heard)

        db.execute(
            """INSERT INTO events (user_id, project_id, title, notes, event_date,
                                   event_time, category, status, source, voice_note_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', 'voice', %s)""",
            (note['user_id'], target, values['title'], values['notes'],
             values['event_date'], values['event_time'], values['category'], note['id']))
        created += 1

    db.execute(
        """UPDATE voice_notes
              SET status = 'done', transcript = %s, processed_at = now(),
                  error = CASE WHEN %s = 0
                               THEN 'Nothing in this one looked like a reminder.' END
            WHERE id = %s""",
        (transcript, created, note['id']))
    db.commit()
    log.info('voice note %d -> %d draft(s)', note['id'], created)


def _loop(database_url, coerce, upload_dir):
    while True:
        try:
            with _connect(database_url) as db:
                requeue_stale(db)
                while True:
                    note = claim_next(db)
                    if note is None:
                        break
                    try:
                        process(db, note, coerce, upload_dir)
                    except Exception as exc:
                        db.rollback()
                        log.exception('voice note %d failed', note['id'])
                        final = note['attempts'] >= MAX_ATTEMPTS
                        db.execute(
                            'UPDATE voice_notes SET status = %s, error = %s, processed_at = %s '
                            'WHERE id = %s',
                            ('failed' if final else 'pending', str(exc)[:500],
                             'now()' if final else None, note['id']))
                        db.commit()
        except Exception:
            log.exception('voice worker loop error')
        time.sleep(POLL_SECONDS)


def start(database_url, coerce, upload_dir):
    """Start the worker once per process."""
    global _running
    with _started:
        if _running:
            return
        _running = True
    thread = threading.Thread(target=_loop, args=(database_url, coerce, upload_dir),
                              name='voice-worker', daemon=True)
    thread.start()
    log.info('voice worker started')
