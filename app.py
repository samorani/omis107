import os

import psycopg
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session, g, flash
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash, check_password_hash

# Load .env for local development. In production the platform supplies the real
# environment, and load_dotenv() leaves already-set variables untouched.
load_dotenv()

DATABASE_URL = os.environ['DATABASE_URL']

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

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
        db.commit()

init_db()

@app.route('/')
def home():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template('home.html', username=session.get('username'))

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

if __name__ == '__main__':
    app.run(debug=True)
    #app.run(host="0.0.0.0", port=80)
