# omis107
my repository for omis107

## Project components
This section describes the components of the project

### Front-end
The first component is the front end

### back-end
Here I amdescribing the logic in the backend

## Deploying on a Google Cloud VM

These steps take a fresh Compute Engine instance to a running copy of the app.
Commands prefixed with `local$` run on your own machine; `vm$` runs over SSH on
the VM.

### 1. Prerequisites

* A Google Cloud project with billing enabled and the Compute Engine API turned on.
* A PostgreSQL database that is reachable from the internet, and its connection
  string. Any managed provider works (Cloud SQL, Neon, Supabase, Render). This is
  the value that goes into `DATABASE_URL`.
* The [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) installed locally,
  or use the Cloud Shell / Console UI instead.

### 2. Create the VM

```
local$ gcloud compute instances create omis107     --zone=us-west1-b     --machine-type=e2-small     --image-family=debian-12     --image-project=debian-cloud     --tags=http-server
```

The `http-server` tag matters: on the default VPC network it attaches the
built-in `default-allow-http` firewall rule, which is what opens port 80 to the
internet. Without the tag the site is unreachable even though the app is running.

Then connect:

```
local$ gcloud compute ssh omis107 --zone=us-west1-b
```

### 3. Install system packages

```
vm$ sudo apt update
vm$ sudo apt install -y python3 python3-venv python3-pip git
```

Debian 12 ships Python 3.11, which is new enough. The app needs 3.10 or later.

### 4. Get the code and install dependencies

```
vm$ git clone https://github.com/samorani/omis107.git
vm$ cd omis107
vm$ python3 -m venv venv
vm$ ./venv/bin/pip install -r requirements.txt
vm$ ./venv/bin/pip install gunicorn
```

`gunicorn` is the production web server. It is installed only on the VM and is
deliberately not in `requirements.txt`, since local development uses Flask's
built-in server instead.

### 5. Configure the environment

The app reads two variables and refuses to start if either is missing. Create the
file directly on the VM:

```
vm$ cp .env.example .env
vm$ nano .env
```

Fill in:

| Variable       | What it is                                                        |
| -------------- | ----------------------------------------------------------------- |
| `DATABASE_URL` | `postgresql://user:password@host/dbname` for your Postgres server. |
| `SECRET_KEY`   | A long random string used to sign session cookies.                 |

Generate a good secret with:

```
vm$ python3 -c "import secrets; print(secrets.token_hex(32))"
```

`.env` is listed in `.gitignore` and must never be committed. Locally,
`python-dotenv` loads this file automatically; in production the same variables
can instead be set in the environment, which takes priority over the file.

Make sure your database provider allows connections from the VM. If it uses an IP
allowlist, add the VM's external address:

```
vm$ curl -s ifconfig.me
```

### 6. Smoke-test before setting up the service

```
vm$ ./venv/bin/gunicorn --bind 0.0.0.0:8000 app:app
```

The app creates the `users` table on startup if it does not exist, so there is no
separate migration step. If this command starts without tracebacks, the database
connection and both environment variables are good. Stop it with `Ctrl+C`. Port
8000 is not open in the firewall, so this only proves the app boots.

### 7. Run it as a systemd service on port 80

Running under systemd means the app restarts on crash and comes back after a VM
reboot. Create the unit file:

```
vm$ sudo nano /etc/systemd/system/omis107.service
```

```ini
[Unit]
Description=omis107 Flask app
After=network.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/omis107
ExecStart=/home/YOUR_USERNAME/omis107/venv/bin/gunicorn --workers 3 --bind 0.0.0.0:80 app:app
Restart=always

# Lets a non-root user bind port 80, so the app does not run as root.
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
```

Replace `YOUR_USERNAME` with the account you are logged in as (`whoami`). Then:

```
vm$ sudo systemctl daemon-reload
vm$ sudo systemctl enable --now omis107
vm$ sudo systemctl status omis107
```

The site is now at `http://EXTERNAL_IP/`, which you can find with:

```
local$ gcloud compute instances describe omis107 --zone=us-west1-b     --format="get(networkInterfaces[0].accessConfigs[0].natIP)"
```

By default this address is ephemeral and changes if the VM is stopped and
started. Promote it to a static IP in the Console under *VPC network > IP
addresses* if you need it to stay put.

### 8. Deploying updates

```
vm$ cd ~/omis107
vm$ git pull
vm$ ./venv/bin/pip install -r requirements.txt
vm$ sudo systemctl restart omis107
```

### Troubleshooting

* **Logs.** `sudo journalctl -u omis107 -f` shows startup errors and tracebacks.
* **Service will not start.** Almost always a missing or malformed `.env`, since
  the app raises `KeyError` at import when `DATABASE_URL` or `SECRET_KEY` is
  absent. Confirm the file sits in `WorkingDirectory`.
* **Page will not load but the service is running.** The firewall tag is missing.
  Check with `gcloud compute instances describe omis107 --zone=us-west1-b
  --format="get(tags.items)"`, and add it if needed:
  `gcloud compute instances add-tags omis107 --zone=us-west1-b --tags=http-server`.
* **Database connection errors.** Check the VM's external IP against your
  database's allowlist, and confirm the provider requires or forbids SSL — append
  `?sslmode=require` to `DATABASE_URL` if it insists on TLS.

## Running locally

```
local$ python -m venv venv
local$ ./venv/Scripts/pip install -r requirements.txt   # venv/bin/pip on macOS or Linux
local$ cp .env.example .env                             # then edit in your own values
local$ ./venv/Scripts/python app.py
```

This starts Flask's development server on http://127.0.0.1:5000 with debug mode
on. It is not suitable for serving real traffic; use the gunicorn setup above for
that.
