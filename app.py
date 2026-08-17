import os
import secrets
from datetime import timedelta
from functools import wraps

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

import db

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128  # guards against long-password hashing DoS
MAX_USERNAME_LENGTH = 32

app = Flask(__name__)

# In production the secret key MUST come from the environment: a generated one
# changes on every restart, which silently logs everybody out.
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,  # not readable from JavaScript
    SESSION_COOKIE_SAMESITE="Lax",  # blocks cross-site cookie sending
    SESSION_COOKIE_SECURE=bool(os.environ.get("SESSION_COOKIE_SECURE")),  # HTTPS only
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)

db.init_app(app)


# --------------------------------------------------------------------------
# CSRF protection
# --------------------------------------------------------------------------

def csrf_token():
    """Per-session token, embedded in every form as a hidden field."""
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def verify_csrf():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(submitted, expected):
            abort(400, "Invalid or missing CSRF token.")


# --------------------------------------------------------------------------
# Current user / access control
# --------------------------------------------------------------------------

@app.before_request
def load_current_user():
    user_id = session.get("user_id")
    g.user = db.get_user_by_id(user_id) if user_id is not None else None


@app.context_processor
def inject_user():
    return {"current_user": g.get("user")}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            flash("Please log in to view that page.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def safe_redirect_target(target, fallback):
    """Only allow relative, same-site redirects (blocks open-redirect abuse)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_registration(username, password, confirm):
    errors = []
    if not username:
        errors.append("Username is required.")
    elif len(username) > MAX_USERNAME_LENGTH:
        errors.append(f"Username must be at most {MAX_USERNAME_LENGTH} characters.")
    elif not all(c.isalnum() or c in "._-" for c in username):
        errors.append("Username may only contain letters, digits, and . _ -")

    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    elif len(password) > MAX_PASSWORD_LENGTH:
        errors.append(f"Password must be at most {MAX_PASSWORD_LENGTH} characters.")

    if password != confirm:
        errors.append("Passwords do not match.")

    return errors


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors = validate_registration(username, password, confirm)
        if errors:
            for error in errors:
                flash(error, "error")
            return render_template("register.html", username=username), 400

        # scrypt is Werkzeug's default: memory-hard, salted per user.
        user_id = db.create_user(username, generate_password_hash(password))
        if user_id is None:
            flash("That username is already taken.", "error")
            return render_template("register.html", username=username), 409

        session.clear()
        session["user_id"] = user_id
        session.permanent = True
        flash("Account created. Welcome!", "success")
        return redirect(url_for("dashboard"))

    return render_template("register.html", username="")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.get("user"):
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db.get_user_by_username(username)
        # Always run a hash comparison so the response time does not reveal
        # whether the username exists.
        stored_hash = user["password_hash"] if user else generate_password_hash(
            secrets.token_hex(16)
        )
        if user and check_password_hash(stored_hash, password):
            # Rotate the session id on privilege change (session fixation).
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            flash("Signed in.", "success")
            return redirect(
                safe_redirect_target(request.args.get("next"), url_for("dashboard"))
            )

        # Deliberately vague: never say which half was wrong.
        flash("Incorrect username or password.", "error")
        return render_template("login.html", username=username), 401

    return render_template("login.html", username="")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html")


if __name__ == "__main__":
    app.run(debug=True)
