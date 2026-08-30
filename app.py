import os
from calendar import Calendar
from datetime import date, timedelta
from functools import wraps

import psycopg
from dotenv import load_dotenv
from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash

# Load .env for local development. In production the platform supplies the real
# environment, and load_dotenv() leaves already-set variables untouched.
load_dotenv()

DATABASE_URL = os.environ['DATABASE_URL']

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

# Fixed list, not a table: contractors should pick from it, never build and
# maintain one. Each value has a matching colour in style.css.
EVENT_CATEGORIES = ['Subcontractor', 'Inspection', 'Delivery', 'Client', 'Payment', 'Other']

# Calendars start on Sunday, the way a US wall calendar does.
CALENDAR = Calendar(firstweekday=6)

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
    """Fetch an event plus its project name, scoped to the logged-in user."""
    event = get_db().execute(
        '''SELECT e.*, p.name AS project_name
           FROM events e
           JOIN projects p ON p.id = e.project_id
           WHERE e.id = %s AND p.user_id = %s''',
        (event_id, session['user_id'])
    ).fetchone()
    if event is None:
        abort(404)
    return event


def parse_event_form(form):
    """Pull an event out of a submitted form.

    Returns (values, error). On error the caller re-renders the form with
    `values`, so the contractor never loses what they typed.
    """
    values = {
        'title': form.get('title', '').strip(),
        'notes': form.get('notes', '').strip(),
        'event_date': form.get('event_date', '').strip(),
        'event_time': form.get('event_time', '').strip(),
        'category': form.get('category', 'Other'),
    }

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
        'title': event['title'],
        'notes': event['notes'] or '',
        'event_date': event['event_date'].isoformat(),
        'event_time': event['event_time'].strftime('%H:%M') if event['event_time'] else '',
        'category': event['category'],
    }


def month_anchor():
    """First day of the month the calendar should show, from ?year=&month=."""
    today = date.today()
    try:
        return date(int(request.args['year']), int(request.args['month']), 1)
    except (KeyError, TypeError, ValueError):
        return date(today.year, today.month, 1)


def shift_month(anchor, delta):
    """First day of the neighbouring month, in whichever direction."""
    if delta < 0:
        return (anchor - timedelta(days=1)).replace(day=1)
    return (anchor + timedelta(days=32)).replace(day=1)


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


@app.template_filter('pretty_date')
def pretty_date(value):
    """2026-09-01 -> Tue, Sep 1. Today and tomorrow get called out by name."""
    if value is None:
        return ''
    today = date.today()
    if value == today:
        return 'Today'
    if value == today + timedelta(days=1):
        return 'Tomorrow'
    return '%s, %s %d' % (DAY_NAMES[(value.weekday() + 1) % 7],
                          MONTH_NAMES[value.month - 1][:3], value.day)


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
            db.execute('INSERT INTO users (username, password) VALUES (%s, %s)', (username, hashed_password))
            db.commit()
            return redirect(url_for('login'))
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

@app.route('/')
@login_required
def home():
    """Everything due soon across every job, plus the list of jobs.

    This is the answer to "what am I forgetting?" -- the reason the app exists.
    """
    db = get_db()
    today = date.today()

    projects = db.execute(
        '''SELECT p.*,
                  (SELECT count(*) FROM events e
                    WHERE e.project_id = p.id AND e.done = FALSE
                      AND e.event_date >= %s) AS open_count
           FROM projects p
           WHERE p.user_id = %s
           ORDER BY p.name''',
        (today, session['user_id'])
    ).fetchall()

    overdue = db.execute(
        '''SELECT e.*, p.name AS project_name
           FROM events e JOIN projects p ON p.id = e.project_id
           WHERE p.user_id = %s AND e.done = FALSE AND e.event_date < %s
           ORDER BY e.event_date DESC, e.event_time NULLS FIRST''',
        (session['user_id'], today)
    ).fetchall()

    upcoming = db.execute(
        '''SELECT e.*, p.name AS project_name
           FROM events e JOIN projects p ON p.id = e.project_id
           WHERE p.user_id = %s AND e.done = FALSE
             AND e.event_date BETWEEN %s AND %s
           ORDER BY e.event_date, e.event_time NULLS FIRST''',
        (session['user_id'], today, today + timedelta(days=13))
    ).fetchall()

    return render_template('home.html', projects=projects, overdue=overdue, upcoming=upcoming)


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
    project = owned_project(project_id)
    db = get_db()
    anchor = month_anchor()

    weeks = CALENDAR.monthdatescalendar(anchor.year, anchor.month)
    span_start, span_end = weeks[0][0], weeks[-1][-1]

    rows = db.execute(
        '''SELECT * FROM events
           WHERE project_id = %s AND event_date BETWEEN %s AND %s
           ORDER BY event_date, event_time NULLS FIRST''',
        (project_id, span_start, span_end)
    ).fetchall()

    # Bucket by day so each calendar cell is a single dictionary lookup.
    events_by_day = {}
    for row in rows:
        events_by_day.setdefault(row['event_date'], []).append(row)

    upcoming = db.execute(
        '''SELECT * FROM events
           WHERE project_id = %s AND done = FALSE AND event_date >= %s
           ORDER BY event_date, event_time NULLS FIRST
           LIMIT 25''',
        (project_id, date.today())
    ).fetchall()

    return render_template(
        'project_detail.html',
        project=project,
        weeks=weeks,
        events_by_day=events_by_day,
        upcoming=upcoming,
        anchor=anchor,
        prev_month=shift_month(anchor, -1),
        next_month=shift_month(anchor, 1),
        today=date.today(),
        day_names=DAY_NAMES,
        month_names=MONTH_NAMES,
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

@app.route('/projects/<int:project_id>/events/new', methods=['GET', 'POST'])
@login_required
def new_event(project_id):
    project = owned_project(project_id)

    if request.method == 'POST':
        values, error = parse_event_form(request.form)
        if error:
            flash(error)
            return render_template('event_form.html', project=project, event=values,
                                   event_id=None, categories=EVENT_CATEGORIES, mode='new'), 400

        db = get_db()
        db.execute(
            '''INSERT INTO events (project_id, title, notes, event_date, event_time, category)
               VALUES (%s, %s, %s, %s, %s, %s)''',
            (project_id, values['title'], values['notes'], values['event_date'],
             values['event_time'] or None, values['category'])
        )
        db.commit()
        return redirect(url_for('project_detail', project_id=project_id))

    # The "+" on a calendar day hands the date in.
    prefill = request.args.get('date', date.today().isoformat())
    try:
        date.fromisoformat(prefill)
    except ValueError:
        prefill = date.today().isoformat()

    return render_template('event_form.html', project=project,
                           event={'event_date': prefill, 'category': 'Other'},
                           event_id=None, categories=EVENT_CATEGORIES, mode='new')


@app.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_event(event_id):
    event = owned_event(event_id)
    project = owned_project(event['project_id'])

    if request.method == 'POST':
        values, error = parse_event_form(request.form)
        if error:
            flash(error)
            return render_template('event_form.html', project=project, event=values,
                                   event_id=event_id, categories=EVENT_CATEGORIES, mode='edit'), 400

        db = get_db()
        db.execute(
            '''UPDATE events SET title = %s, notes = %s, event_date = %s,
                                 event_time = %s, category = %s
               WHERE id = %s''',
            (values['title'], values['notes'], values['event_date'],
             values['event_time'] or None, values['category'], event_id)
        )
        db.commit()
        return redirect(url_for('project_detail', project_id=event['project_id']))

    return render_template('event_form.html', project=project,
                           event=event_form_values(event), event_id=event_id,
                           categories=EVENT_CATEGORIES, mode='edit')


@app.route('/events/<int:event_id>/toggle', methods=['POST'])
@login_required
def toggle_event(event_id):
    event = owned_event(event_id)
    db = get_db()
    db.execute('UPDATE events SET done = NOT done WHERE id = %s', (event_id,))
    db.commit()
    if request.form.get('next') == 'home':
        return redirect(url_for('home'))
    return redirect(url_for('project_detail', project_id=event['project_id']))


@app.route('/events/<int:event_id>/delete', methods=['POST'])
@login_required
def delete_event(event_id):
    event = owned_event(event_id)
    db = get_db()
    db.execute('DELETE FROM events WHERE id = %s', (event_id,))
    db.commit()
    flash('Reminder deleted.')
    return redirect(url_for('project_detail', project_id=event['project_id']))


if __name__ == '__main__':
    app.run(debug=True)
    #app.run(host="0.0.0.0", port=80)
