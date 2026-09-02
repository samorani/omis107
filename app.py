import os
import uuid
from datetime import date, time, timedelta
from functools import wraps

import psycopg
from dotenv import load_dotenv
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash

import ai

# Load .env for local development. In production the platform supplies the real
# environment, and load_dotenv() leaves already-set variables untouched.
load_dotenv()

DATABASE_URL = os.environ['DATABASE_URL']

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

# Voice memos land here. Recordings are kept so a bad parse can be replayed
# against a better model later, which is the whole reason we upload audio
# instead of letting the browser transcribe it.
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Matches the transcription API's own 25 MB ceiling, so an oversized upload
# fails here instead of after we have paid to move it.
app.config['MAX_CONTENT_LENGTH'] = ai.MAX_AUDIO_BYTES

# MediaRecorder gives webm on Chrome and mp4 on Safari; both are accepted.
AUDIO_EXTENSIONS = {'audio/webm': '.webm', 'audio/ogg': '.ogg',
                    'audio/mp4': '.m4a', 'audio/mpeg': '.mp3', 'audio/wav': '.wav'}

# Fixed list, not a table: contractors should pick from it, never build and
# maintain one. Each value has a matching colour in style.css.
EVENT_CATEGORIES = ['Subcontractor', 'Inspection', 'Delivery', 'Client', 'Payment', 'Other']

DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July',
               'August', 'September', 'October', 'November', 'December']


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


def init_db():
    with app.app_context():
        db = get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                client_name TEXT,
                address TEXT,
                phone TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                title TEXT NOT NULL,
                notes TEXT,
                event_date DATE NOT NULL,
                event_time TIME,
                category TEXT NOT NULL DEFAULT 'Other',
                done BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS voice_notes (
                id SERIAL PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                transcript TEXT NOT NULL,
                audio_filename TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
        ''')
        # The table doubles as the work queue: a recording is durable the moment
        # it is uploaded, so the browser can leave and the work still happens.
        db.execute("ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'done'")
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS error TEXT')
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0')
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ')
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ')
        db.execute('ALTER TABLE voice_notes ALTER COLUMN transcript DROP NOT NULL')
        db.execute('CREATE INDEX IF NOT EXISTS voice_notes_status_idx ON voice_notes (status)')
        # Each recording carries an id made by the browser. If the phone's
        # connection drops mid-upload the browser re-sends the very same body,
        # id included -- so this index is what stops one memo becoming two.
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS client_token TEXT')
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS voice_notes_client_token_idx '
                   'ON voice_notes (user_id, client_token) WHERE client_token IS NOT NULL')

        # A memo can now be recorded from the dashboard, before anyone has said
        # which job it belongs to -- so ownership lives on the row itself and the
        # job becomes optional until the draft is confirmed.
        db.execute('ALTER TABLE voice_notes ADD COLUMN IF NOT EXISTS user_id INTEGER '
                   'REFERENCES users(id) ON DELETE CASCADE')
        db.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS user_id INTEGER '
                   'REFERENCES users(id) ON DELETE CASCADE')
        db.execute('''UPDATE voice_notes v SET user_id = p.user_id
                        FROM projects p WHERE p.id = v.project_id AND v.user_id IS NULL''')
        db.execute('''UPDATE events e SET user_id = p.user_id
                        FROM projects p WHERE p.id = e.project_id AND e.user_id IS NULL''')
        db.execute('ALTER TABLE voice_notes ALTER COLUMN project_id DROP NOT NULL')
        db.execute('ALTER TABLE events ALTER COLUMN project_id DROP NOT NULL')
        db.execute('CREATE INDEX IF NOT EXISTS events_user_idx ON events (user_id, event_date)')
        # Memos are written up in-request now; nothing will ever claim a row left
        # queued by the old background worker, so mark those for a manual retry.
        db.execute("""UPDATE voice_notes
                         SET status = 'failed',
                             error = 'Left over from background processing -- try again.'
                       WHERE status IN ('pending', 'processing')""")
        # Added after the events table already existed, so these run as ALTERs.
        # status: 'draft' until a person confirms it, then 'confirmed'.
        db.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'confirmed'")
        db.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual'")
        db.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS voice_note_id INTEGER '
                   'REFERENCES voice_notes(id) ON DELETE SET NULL')
        # Null until the reminder is checked off; cleared again if it is reopened.
        db.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ')
        db.execute('CREATE INDEX IF NOT EXISTS events_project_date_idx ON events (project_id, event_date)')
        db.commit()


init_db()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return view(*args, **kwargs)
    return wrapped


def owned_project(project_id):
    """Fetch a project, but only if it belongs to the logged-in user."""
    project = get_db().execute(
        'SELECT * FROM projects WHERE id = %s AND user_id = %s',
        (project_id, session['user_id'])
    ).fetchone()
    if project is None:
        abort(404)
    return project


def owned_event(event_id):
    """Fetch an event plus its job name, scoped to the logged-in user.

    LEFT JOIN, not JOIN: a voice draft may not have been assigned to a job yet.
    """
    event = get_db().execute(
        '''SELECT e.*, p.name AS project_name
           FROM events e
           LEFT JOIN projects p ON p.id = e.project_id
           WHERE e.id = %s AND e.user_id = %s''',
        (event_id, session['user_id'])
    ).fetchone()
    if event is None:
        abort(404)
    return event


def owned_note(note_id):
    note = get_db().execute(
        'SELECT * FROM voice_notes WHERE id = %s AND user_id = %s',
        (note_id, session['user_id'])
    ).fetchone()
    if note is None:
        abort(404)
    return note


def write_up_memo(note_id):
    """Transcribe a recording, parse it, and file the drafts. Runs in-request.

    Returns the number of drafts created. Raises on failure -- the caller
    records the message against the note so the contractor can retry.
    """
    db = get_db()
    note = db.execute('SELECT * FROM voice_notes WHERE id = %s', (note_id,)).fetchone()

    project_name = None
    if note['project_id'] is not None:
        row = db.execute('SELECT name FROM projects WHERE id = %s',
                         (note['project_id'],)).fetchone()
        project_name = row['name'] if row else None

    # Memos recorded from the dashboard carry no job, so offer the model the
    # names it could be talking about.
    jobs = db.execute('SELECT id, name FROM projects WHERE user_id = %s ORDER BY name',
                      (note['user_id'],)).fetchall()
    by_name = {j['name'].strip().lower(): j['id'] for j in jobs}

    transcript = ai.transcribe(os.path.join(UPLOAD_DIR, note['audio_filename']))
    if not transcript:
        raise ValueError('That recording came back empty -- try again, closer to the mic.')

    candidates = ai.parse_transcript(transcript, project_name=project_name,
                                     job_names=[j['name'] for j in jobs])

    created = 0
    for candidate in candidates:
        values = coerce_parsed_event(candidate)
        if values is None:                   # unusable date -- drop rather than guess
            continue

        # The memo's own job wins; otherwise take the one the model named, but
        # only on an exact (case-insensitive) match against a job that exists.
        # A near-miss becomes null, which just asks the person to pick.
        target = note['project_id']
        if target is None:
            target = by_name.get((candidate.get('job') or '').strip().lower())

        db.execute(
            """INSERT INTO events (user_id, project_id, title, notes, event_date,
                                   event_time, category, status, source, voice_note_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', 'voice', %s)""",
            (note['user_id'], target, values['title'], values['notes'],
             values['event_date'], values['event_time'], values['category'], note_id))
        created += 1

    db.execute(
        """UPDATE voice_notes
              SET status = 'done', transcript = %s, processed_at = now(),
                  error = CASE WHEN %s = 0
                               THEN 'Nothing in this one looked like a reminder.' END
            WHERE id = %s""",
        (transcript, created, note_id))
    db.commit()
    return created


def memo_outcome(note, project_id, duplicate=False):
    """The answer the browser gets for a memo, whether fresh or a repeat."""
    if note['status'] == 'failed':
        return jsonify(error=note['error'] or 'That one did not go through.'), 502

    created = get_db().execute(
        'SELECT count(*) AS n FROM events WHERE voice_note_id = %s', (note['id'],)
    ).fetchone()['n']

    return jsonify(created=created,
                   duplicate=duplicate,
                   processing=note['status'] == 'processing',
                   drafts=drafts_token(draft_rows(project_id)))


def draft_rows(project_id=None):
    """Drafts awaiting confirmation, for the dashboard or for one job."""
    if project_id is None:
        return get_db().execute(
            """SELECT e.*, p.name AS project_name, v.transcript
                 FROM events e
                 LEFT JOIN projects p ON p.id = e.project_id
                 LEFT JOIN voice_notes v ON v.id = e.voice_note_id
                WHERE e.user_id = %s AND e.status = 'draft'
                ORDER BY e.created_at DESC, e.event_date""",
            (session['user_id'],)
        ).fetchall()
    return get_db().execute(
        """SELECT e.*, p.name AS project_name, v.transcript
             FROM events e
             LEFT JOIN projects p ON p.id = e.project_id
             LEFT JOIN voice_notes v ON v.id = e.voice_note_id
            WHERE e.project_id = %s AND e.status = 'draft'
            ORDER BY e.created_at DESC, e.event_date""",
        (project_id,)
    ).fetchall()


def drafts_token(drafts):
    """Cheap fingerprint the browser can compare to spot a change."""
    return '%d:%d' % (len(drafts), max([d['id'] for d in drafts], default=0))


def user_projects():
    return get_db().execute(
        'SELECT id, name FROM projects WHERE user_id = %s ORDER BY name',
        (session['user_id'],)
    ).fetchall()


def parse_event_form(form, default_project_id=None):
    """Pull an event out of a submitted form.

    Returns (values, error). On error the caller re-renders the form with
    `values`, so the contractor never loses what they typed.
    """
    # A form posted from inside a job already says which job it is; the
    # selector only has to be answered when nothing else implies one.
    values = {
        'project_id': form.get('project_id', '').strip() or (
            str(default_project_id) if default_project_id else ''),
        'title': form.get('title', '').strip(),
        'notes': form.get('notes', '').strip(),
        'event_date': form.get('event_date', '').strip(),
        'event_time': form.get('event_time', '').strip(),
        'category': form.get('category', 'Other'),
    }

    if not values['project_id'].isdigit():
        return values, 'Please choose which job this belongs to.'
    if not values['title']:
        return values, 'Please write what the reminder is about.'
    if not values['event_date']:
        return values, 'Please pick a date.'
    try:
        date.fromisoformat(values['event_date'])
    except ValueError:
        return values, 'That date does not look right.'
    if values['category'] not in EVENT_CATEGORIES:
        values['category'] = 'Other'

    return values, None


def event_form_values(event):
    """Turn a database row into the string shape the event form expects."""
    return {
        'project_id': str(event['project_id']) if event['project_id'] else '',
        'title': event['title'],
        'notes': event['notes'] or '',
        'event_date': event['event_date'].isoformat(),
        'event_time': event['event_time'].strftime('%H:%M') if event['event_time'] else '',
        'category': event['category'],
    }


def coerce_parsed_event(candidate):
    """Validate one model-produced event before it can reach the database.

    Model output is untrusted input like any other. A bad date drops the event
    (returns None) rather than guessing at one; a bad category falls back to
    'Other', since being wrong about the colour is harmless.
    """
    title = (candidate.get('title') or '').strip()
    raw_date = (candidate.get('event_date') or '').strip()
    if not title or not raw_date:
        return None
    try:
        date.fromisoformat(raw_date)
    except ValueError:
        return None

    raw_time = (candidate.get('event_time') or '').strip()
    try:
        event_time = time.fromisoformat(raw_time) if raw_time else None
    except ValueError:
        event_time = None

    category = candidate.get('category')
    if category not in EVENT_CATEGORIES:
        category = 'Other'

    return {
        'title': title[:200],
        'notes': (candidate.get('notes') or '').strip(),
        'event_date': raw_date,
        'event_time': event_time,
        'category': category,
    }


def voice_transcript(event):
    """The memo an event came from, or None if it was typed by hand."""
    if not event.get('voice_note_id'):
        return None
    row = get_db().execute(
        'SELECT transcript FROM voice_notes WHERE id = %s', (event['voice_note_id'],)
    ).fetchone()
    return row['transcript'] if row else None


# --------------------------------------------------------------------------
# Template filters
# --------------------------------------------------------------------------

@app.template_filter('pretty_time')
def pretty_time(value):
    """08:00 -> 8:00 AM. Blank when an event has no set time."""
    if value is None:
        return ''
    hour = value.hour % 12 or 12
    suffix = 'AM' if value.hour < 12 else 'PM'
    return '%d:%02d %s' % (hour, value.minute, suffix)


@app.template_filter('day_label')
def day_label(value):
    """TODAY / TOMORROW / TUE -- the top line of the date block."""
    if value is None:
        return ''
    today = date.today()
    if value == today:
        return 'Today'
    if value == today + timedelta(days=1):
        return 'Tomorrow'
    return DAY_NAMES[(value.weekday() + 1) % 7]


@app.template_filter('date_label')
def date_label(value):
    """Sep 4 -- the big line of the date block."""
    if value is None:
        return ''
    return '%s %d' % (MONTH_NAMES[value.month - 1][:3], value.day)


@app.template_filter('pretty_stamp')
def pretty_stamp(value):
    """Timestamp -> 'Sep 1, 2026'. Blank when never set."""
    if value is None:
        return ''
    return '%s %d, %d' % (MONTH_NAMES[value.month - 1][:3], value.day, value.year)


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        hashed_password = generate_password_hash(password)

        db = get_db()
        try:
            user = db.execute(
                'INSERT INTO users (username, password) VALUES (%s, %s) RETURNING id, username',
                (username, hashed_password)
            ).fetchone()
            db.commit()
            # Straight in -- making someone type the same details twice to reach
            # an empty dashboard is a pointless hurdle.
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('home'))
        except psycopg.errors.UniqueViolation:
            db.rollback()
            flash('That username is already taken.')
            return render_template('register.html'), 400

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = %s', (username,)).fetchone()

        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('home'))

        flash('Invalid username or password.')
        return render_template('login.html'), 400

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

# How far ahead the dashboard looks. Anything past the window is still there,
# a click away, rather than hidden behind pagination.
HORIZONS = {
    '2w': ('2 weeks', 13),
    '6w': ('6 weeks', 41),
    'all': ('Everything', None),
}


@app.route('/')
@login_required
def home():
    """Record a memo, and see what is coming up across every job."""
    db = get_db()
    today = date.today()

    projects = db.execute(
        """SELECT p.*,
                  (SELECT count(*) FROM events e
                    WHERE e.project_id = p.id AND e.done = FALSE
                      AND e.status = 'confirmed' AND e.event_date >= %s) AS open_count
             FROM projects p
            WHERE p.user_id = %s
            ORDER BY p.name""",
        (today, session['user_id'])
    ).fetchall()

    # Nothing works before there is a job to hang it on -- a reminder needs one,
    # and so does a voice memo. So a new account gets one door, not a dashboard
    # of empty panels.
    if not projects:
        return render_template('welcome.html')

    horizon = request.args.get('horizon', '2w')
    if horizon not in HORIZONS:
        horizon = '2w'
    _, days = HORIZONS[horizon]

    drafts = draft_rows()

    overdue = db.execute(
        """SELECT e.*, p.name AS project_name
             FROM events e LEFT JOIN projects p ON p.id = e.project_id
            WHERE e.user_id = %s AND e.done = FALSE AND e.status = 'confirmed'
              AND e.event_date < %s
            ORDER BY e.event_date DESC, e.event_time NULLS FIRST""",
        (session['user_id'], today)
    ).fetchall()

    params = [session['user_id'], today]
    window = ''
    if days is not None:
        window = 'AND e.event_date <= %s'
        params.append(today + timedelta(days=days))

    upcoming = db.execute(
        """SELECT e.*, p.name AS project_name
             FROM events e LEFT JOIN projects p ON p.id = e.project_id
            WHERE e.user_id = %s AND e.done = FALSE AND e.status = 'confirmed'
              AND e.event_date >= %s """ + window + """
            ORDER BY e.event_date, e.event_time NULLS FIRST""",
        params
    ).fetchall()

    # How many sit beyond the current window, so the wider views can say so.
    beyond = db.execute(
        """SELECT count(*) AS n FROM events e
            WHERE e.user_id = %s AND e.done = FALSE AND e.status = 'confirmed'
              AND e.event_date > %s""",
        (session['user_id'], today + timedelta(days=days) if days is not None else today)
    ).fetchone()['n'] if days is not None else 0

    problems = db.execute(
        """SELECT v.*, p.name AS project_name
             FROM voice_notes v LEFT JOIN projects p ON p.id = v.project_id
            WHERE v.user_id = %s AND v.error IS NOT NULL
              AND v.status IN ('failed', 'done')
            ORDER BY v.created_at DESC LIMIT 5""",
        (session['user_id'],)
    ).fetchall()

    return render_template('home.html', projects=projects, drafts=drafts,
                           example_job=projects[0]['name'],
                           drafts_token=drafts_token(drafts),
                           overdue=overdue, upcoming=upcoming, problems=problems, horizon=horizon, horizons=HORIZONS,
                           beyond=beyond, ai_provider=ai.provider_name())


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

HISTORY_FILTERS = {
    'done': "AND e.done = TRUE",
    'missed': "AND e.done = FALSE AND e.event_date < %(today)s",
    'all': "",
}


@app.route('/history')
@app.route('/projects/<int:project_id>/history')
@login_required
def history(project_id=None):
    """Everything that has already happened, completed or not.

    Deliberately shows checked-off items -- "did I ever get the framing
    inspection done, and when?" is a question the calendar cannot answer once
    the date has passed.
    """
    project = owned_project(project_id) if project_id else None
    show = request.args.get('show', 'all')
    if show not in HISTORY_FILTERS:
        show = 'all'

    params = {'user_id': session['user_id'], 'today': date.today()}
    where = ['p.user_id = %(user_id)s', "e.status = 'confirmed'"]
    if project_id:
        where.append('e.project_id = %(project_id)s')
        params['project_id'] = project_id

    rows = get_db().execute(
        """SELECT e.*, p.name AS project_name
             FROM events e JOIN projects p ON p.id = e.project_id
            WHERE """ + ' AND '.join(where) + ' ' + HISTORY_FILTERS[show] + """
            ORDER BY e.event_date DESC, e.event_time DESC NULLS LAST""",
        params
    ).fetchall()

    return render_template('history.html', events=rows, project=project,
                           show=show, today=date.today())


# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

@app.route('/projects/new', methods=['GET', 'POST'])
@login_required
def new_project():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Please give the job a name.')
            return render_template('project_form.html', project=request.form, mode='new'), 400

        db = get_db()
        row = db.execute(
            '''INSERT INTO projects (user_id, name, client_name, address, phone, notes)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
            (session['user_id'], name,
             request.form.get('client_name', '').strip(),
             request.form.get('address', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('notes', '').strip())
        ).fetchone()
        db.commit()
        return redirect(url_for('project_detail', project_id=row['id']))

    return render_template('project_form.html', project={}, mode='new')


@app.route('/projects/<int:project_id>')
@login_required
def project_detail(project_id):
    """One job: what needs doing, soonest first.

    This used to be a month calendar. A grid is good for spotting a free
    Thursday; it is bad at the question actually being asked here, which is
    "what is next on this job" -- so it is a list, like the dashboard.
    """
    project = owned_project(project_id)
    db = get_db()
    today = date.today()

    overdue = db.execute(
        """SELECT * FROM events
             WHERE project_id = %s AND done = FALSE AND status = 'confirmed'
               AND event_date < %s
             ORDER BY event_date DESC, event_time NULLS FIRST""",
        (project_id, today)
    ).fetchall()

    upcoming = db.execute(
        """SELECT * FROM events
             WHERE project_id = %s AND done = FALSE AND status = 'confirmed'
               AND event_date >= %s
             ORDER BY event_date, event_time NULLS FIRST""",
        (project_id, today)
    ).fetchall()

    drafts = draft_rows(project_id)

    problems = db.execute(
        """SELECT * FROM voice_notes
             WHERE project_id = %s AND error IS NOT NULL
               AND status IN ('failed', 'done')
             ORDER BY created_at DESC LIMIT 5""",
        (project_id,)
    ).fetchall()

    return render_template(
        'project_detail.html',
        project=project,
        overdue=overdue,
        upcoming=upcoming,
        drafts=drafts,
        drafts_token=drafts_token(drafts),
        example_job=project['name'],
        projects=user_projects(),
        problems=problems,
        ai_provider=ai.provider_name(),
    )


@app.route('/projects/<int:project_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_project(project_id):
    project = owned_project(project_id)

    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('Please give the job a name.')
            return render_template('project_form.html', project=request.form, mode='edit'), 400

        db = get_db()
        db.execute(
            '''UPDATE projects SET name = %s, client_name = %s, address = %s,
                                   phone = %s, notes = %s
               WHERE id = %s AND user_id = %s''',
            (name,
             request.form.get('client_name', '').strip(),
             request.form.get('address', '').strip(),
             request.form.get('phone', '').strip(),
             request.form.get('notes', '').strip(),
             project_id, session['user_id'])
        )
        db.commit()
        return redirect(url_for('project_detail', project_id=project_id))

    return render_template('project_form.html', project=project, mode='edit')


@app.route('/projects/<int:project_id>/delete', methods=['POST'])
@login_required
def delete_project(project_id):
    owned_project(project_id)
    db = get_db()
    # The job's events go with it, via ON DELETE CASCADE.
    db.execute('DELETE FROM projects WHERE id = %s AND user_id = %s', (project_id, session['user_id']))
    db.commit()
    flash('Job deleted.')
    return redirect(url_for('home'))


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

@app.route('/reminders/new', methods=['GET', 'POST'])
@app.route('/projects/<int:project_id>/events/new', methods=['GET', 'POST'])
@login_required
def new_event(project_id=None):
    # Reached from the dashboard there is no job yet, so the form asks for one.
    project = owned_project(project_id) if project_id else None

    if request.method == 'POST':
        values, error = parse_event_form(request.form, default_project_id=project_id)
        if error:
            flash(error)
            return render_template('event_form.html', project=project, event=values,
                                   event_id=None, categories=EVENT_CATEGORIES,
                                   projects=user_projects(), mode='new'), 400

        # Written from the dashboard there is no job in the URL, so land on
        # whichever one the form picked.
        target = owned_project(int(values['project_id']))['id']
        db = get_db()
        db.execute(
            '''INSERT INTO events (user_id, project_id, title, notes, event_date,
                                   event_time, category)
               VALUES (%s, %s, %s, %s, %s, %s, %s)''',
            (session['user_id'], target, values['title'], values['notes'],
             values['event_date'], values['event_time'] or None, values['category'])
        )
        db.commit()
        return redirect(url_for('project_detail', project_id=target))

    prefill = request.args.get('date', date.today().isoformat())
    try:
        date.fromisoformat(prefill)
    except ValueError:
        prefill = date.today().isoformat()

    return render_template('event_form.html', project=project,
                           event={'event_date': prefill, 'category': 'Other',
                                  'project_id': str(project_id) if project_id else ''},
                           event_id=None, categories=EVENT_CATEGORIES,
                           projects=user_projects(), mode='new')


@app.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_event(event_id):
    event = owned_event(event_id)
    project = owned_project(event['project_id']) if event['project_id'] else None

    if request.method == 'POST':
        values, error = parse_event_form(request.form,
                                         default_project_id=event['project_id'])
        if error:
            flash(error)
            return render_template('event_form.html', project=project, event=values,
                                   event_id=event_id, categories=EVENT_CATEGORIES,
                                   projects=user_projects(), mode='edit',
                                   is_draft=event['status'] == 'draft',
                                   transcript=voice_transcript(event)), 400

        db = get_db()
        # Saving a draft is how it gets confirmed -- reviewing it *is* the
        # approval, so there is no second button to forget to press.
        target = owned_project(int(values['project_id']))['id']
        db.execute(
            '''UPDATE events SET project_id = %s, title = %s, notes = %s, event_date = %s,
                                 event_time = %s, category = %s, status = 'confirmed'
               WHERE id = %s''',
            (target, values['title'], values['notes'], values['event_date'],
             values['event_time'] or None, values['category'], event_id)
        )
        db.commit()
        if event['status'] == 'draft':
            flash('Reminder confirmed.')
        return redirect(url_for('project_detail', project_id=target))

    return render_template('event_form.html', project=project,
                           event=event_form_values(event), event_id=event_id,
                           categories=EVENT_CATEGORIES, projects=user_projects(),
                           mode='edit', is_draft=event['status'] == 'draft',
                           transcript=voice_transcript(event))


@app.route('/events/<int:event_id>/toggle', methods=['POST'])
@login_required
def toggle_event(event_id):
    event = owned_event(event_id)
    db = get_db()
    # In an UPDATE, `done` on the right-hand side is still the OLD value, so
    # this stamps now() when checking off and clears it when reopening.
    db.execute(
        '''UPDATE events
              SET done = NOT done,
                  completed_at = CASE WHEN done THEN NULL ELSE now() END
            WHERE id = %s''',
        (event_id,)
    )
    db.commit()
    # Where to land afterwards. Only named destinations are honoured -- never a
    # URL from the form, which would be an open redirect.
    destination = request.form.get('next')
    if destination == 'home':
        return redirect(url_for('home'))
    if destination == 'history':
        scope = request.form.get('project_id')
        return redirect(url_for('history',
                                project_id=int(scope) if scope and scope.isdigit() else None,
                                show=request.form.get('show') or None))
    return redirect(url_for('project_detail', project_id=event['project_id']))


@app.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
def delete_event(event_id):
    event = owned_event(event_id)
    db = get_db()
    db.execute('DELETE FROM events WHERE id = %s', (event_id,))
    db.commit()
    flash('Reminder deleted.')
    return redirect(_back_to(event['project_id']))


@app.route('/events/<int:event_id>/confirm', methods=['POST'])
@login_required
def confirm_event(event_id):
    """Accept a draft, assigning a job if it does not have one yet."""
    event = owned_event(event_id)
    db = get_db()

    project_id = event['project_id']
    chosen = request.form.get('project_id', '').strip()
    if chosen.isdigit():
        project_id = owned_project(int(chosen))['id']

    if project_id is None:
        flash('Pick which job that one belongs to first.')
        return redirect(url_for('home'))

    db.execute("UPDATE events SET status = 'confirmed', project_id = %s WHERE id = %s",
               (project_id, event_id))
    db.commit()
    flash('Reminder confirmed.')
    if request.form.get('next') == 'home':
        return redirect(url_for('home'))
    return redirect(url_for('project_detail', project_id=project_id))


# --------------------------------------------------------------------------
# Voice memos
# --------------------------------------------------------------------------

@app.route('/voice', methods=['POST'])
@app.route('/projects/<int:project_id>/voice', methods=['POST'])
@login_required
def voice_memo(project_id=None):
    """Record, write up, answer. One memo at a time.

    Processing happens here rather than on a queue: the audio only exists on
    this machine, so the work has to happen where the file is.
    """
    if project_id is not None:
        owned_project(project_id)

    # The browser stamps every recording with an id and repeats it verbatim on
    # a retry, so the same recording can never be written up twice.
    token = (request.form.get('client_token') or '').strip()[:64] or None

    db = get_db()
    if token:
        seen = db.execute(
            'SELECT * FROM voice_notes WHERE user_id = %s AND client_token = %s',
            (session['user_id'], token)
        ).fetchone()
        if seen is not None:
            app.logger.info('ignoring repeat upload of memo %s', seen['id'])
            return memo_outcome(seen, project_id, duplicate=True)

    upload = request.files.get('audio')
    if upload is None or not upload.filename:
        return jsonify(error='No recording was received.'), 400

    mime = (upload.mimetype or '').split(';')[0]
    extension = AUDIO_EXTENSIONS.get(mime, '.webm')
    filename = '%s-%s%s' % (project_id or 'inbox', uuid.uuid4().hex, extension)
    upload.save(os.path.join(UPLOAD_DIR, filename))

    try:
        note = db.execute(
            "INSERT INTO voice_notes (user_id, project_id, audio_filename, status, client_token) "
            "VALUES (%s, %s, %s, 'processing', %s) RETURNING id",
            (session['user_id'], project_id, filename, token)
        ).fetchone()
        db.commit()
    except psycopg.errors.UniqueViolation:
        # Two copies of the same upload arrived close enough together to race.
        db.rollback()
        os.remove(os.path.join(UPLOAD_DIR, filename))
        seen = db.execute(
            'SELECT * FROM voice_notes WHERE user_id = %s AND client_token = %s',
            (session['user_id'], token)
        ).fetchone()
        app.logger.info('dropped a racing duplicate of memo %s', seen['id'])
        return memo_outcome(seen, project_id, duplicate=True)

    try:
        created = write_up_memo(note['id'])
    except Exception as exc:
        db.rollback()
        app.logger.exception('writing up memo %s failed', note['id'])
        db.execute("UPDATE voice_notes SET status = 'failed', error = %s, processed_at = now() "
                   'WHERE id = %s', (str(exc)[:500], note['id']))
        db.commit()
        return jsonify(error=str(exc)[:200]), 502

    return jsonify(created=created,
                   drafts=drafts_token(draft_rows(project_id)))


@app.route('/voice/status')
@app.route('/projects/<int:project_id>/voice/status')
@login_required
def voice_status(project_id=None):
    """What the page asks for when it wakes up: did I miss anything?

    A phone suspends the page while the screen is off, so the answer to the
    upload request can arrive at a page that is not running to receive it. On
    waking, the page asks this instead of sitting there stale.
    """
    db = get_db()
    if project_id is not None:
        owned_project(project_id)
        busy = db.execute(
            "SELECT count(*) AS n FROM voice_notes "
            "WHERE project_id = %s AND status = 'processing'", (project_id,)
        ).fetchone()['n']
    else:
        busy = db.execute(
            "SELECT count(*) AS n FROM voice_notes "
            "WHERE user_id = %s AND status = 'processing'", (session['user_id'],)
        ).fetchone()['n']

    return jsonify(processing=busy, drafts=drafts_token(draft_rows(project_id)))


@app.route('/fragments/drafts')
@app.route('/projects/<int:project_id>/fragments/drafts')
@login_required
def drafts_fragment(project_id=None):
    """Just the "Needs your OK" panel, for the page to swap in place.

    Rendering it server-side means the fragment and the full page come from one
    template -- there is no second, drifting copy of the markup in JavaScript.
    """
    if project_id is not None:
        owned_project(project_id)
    drafts = draft_rows(project_id)
    return render_template('_drafts_panel.html', drafts=drafts,
                           projects=user_projects(),
                           show_project=project_id is None,
                           back='home' if project_id is None else '')


@app.route('/voice-notes/<int:note_id>/dismiss', methods=['POST'])
@login_required
def dismiss_note(note_id):
    """Clear a failed memo the contractor has read and does not want to retry."""
    note = owned_note(note_id)
    db = get_db()
    db.execute("UPDATE voice_notes SET status = 'dismissed', error = NULL WHERE id = %s", (note_id,))
    db.commit()
    return redirect(_back_to(note['project_id']))


@app.route('/voice-notes/<int:note_id>/retry', methods=['POST'])
@login_required
def retry_note(note_id):
    note = owned_note(note_id)
    db = get_db()
    db.execute("UPDATE voice_notes SET status = 'processing', error = NULL WHERE id = %s",
               (note_id,))
    db.commit()
    try:
        created = write_up_memo(note_id)
    except Exception as exc:
        db.rollback()
        app.logger.exception('retry of memo %s failed', note_id)
        db.execute("UPDATE voice_notes SET status = 'failed', error = %s, processed_at = now() "
                   'WHERE id = %s', (str(exc)[:500], note_id))
        db.commit()
        flash('That memo still would not go through.')
    else:
        flash('Added %d draft reminder%s.' % (created, '' if created == 1 else 's'))
    return redirect(_back_to(note['project_id']))


def _back_to(project_id):
    return (url_for('project_detail', project_id=project_id) if project_id
            else url_for('home'))


if __name__ == '__main__':
    app.run(debug=True)
    #app.run(host="0.0.0.0", port=80)
