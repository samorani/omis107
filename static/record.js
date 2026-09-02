// Voice memo recording.
//
// The browser only captures audio -- transcription happens server-side, so this
// works in every browser with MediaRecorder, not just the ones with a speech
// API. getUserMedia needs a secure context: fine on localhost, needs HTTPS once
// deployed.
//
// Uploads are fire-and-forget. The server stores the audio, queues it, and
// answers 202 immediately, so stopping a recording is instant. A one-line
// "writing up" note shows while memos are still being processed; the page
// re-checks and reloads itself once the drafts are ready -- including when the
// phone has been asleep in the meantime.
document.addEventListener('DOMContentLoaded', function () {
    var button = document.getElementById('record-btn');
    if (!button) return;

    var status = document.getElementById('record-status');
    var form = document.getElementById('record-form');
    var queue = document.getElementById('record-queue');
    var label = button.querySelector('.record-label');

    if (!(navigator.mediaDevices && window.MediaRecorder)) {
        button.disabled = true;
        say('This browser cannot record audio. Try Chrome, Edge, or Safari.');
        return;
    }

    var recorder = null;
    var chunks = [];
    var timer = null;
    var startedAt = 0;
    var inFlight = 0;        // uploads accepted but not yet finished server-side
    var poller = null;

    function say(message) {
        if (status) status.textContent = message;
    }

    // "Writing it up" is what a foreman calls turning site notes into something
    // written down -- which is exactly what is happening to the recording.
    function showQueue() {
        if (!queue) return;
        if (inFlight > 0) {
            queue.hidden = false;
            queue.textContent = inFlight === 1
                ? 'Writing up 1 memo…'
                : 'Writing up ' + inFlight + ' memos…';
        } else {
            queue.hidden = true;
        }
    }

    function pickMimeType() {
        // Chrome and Firefox give webm; Safari gives mp4. Both are accepted.
        var candidates = ['audio/webm', 'audio/mp4', 'audio/ogg'];
        for (var i = 0; i < candidates.length; i++) {
            if (MediaRecorder.isTypeSupported(candidates[i])) return candidates[i];
        }
        return '';
    }

    function tick() {
        var seconds = Math.floor((Date.now() - startedAt) / 1000);
        var mins = Math.floor(seconds / 60);
        var secs = seconds % 60;
        say('Recording ' + mins + ':' + (secs < 10 ? '0' : '') + secs + ' - tap to stop');
    }

    async function start() {
        var stream;
        try {
            stream = await navigator.mediaDevices.getUserMedia({audio: true});
        } catch (err) {
            say(err && err.name === 'NotAllowedError'
                ? 'Microphone permission was denied. Allow it in your browser settings.'
                : 'Could not reach the microphone.');
            return;
        }

        var mimeType = pickMimeType();
        recorder = new MediaRecorder(stream, mimeType ? {mimeType: mimeType} : undefined);
        chunks = [];

        recorder.addEventListener('dataavailable', function (event) {
            if (event.data && event.data.size > 0) chunks.push(event.data);
        });

        recorder.addEventListener('stop', function () {
            stream.getTracks().forEach(function (track) { track.stop(); });
            upload(new Blob(chunks, {type: recorder.mimeType || 'audio/webm'}));
        });

        recorder.start();
        startedAt = Date.now();
        timer = setInterval(tick, 250);
        tick();

        button.classList.add('is-recording');
        label.textContent = 'Stop';
    }

    function stop() {
        clearInterval(timer);
        button.classList.remove('is-recording');
        label.textContent = 'Record a memo';
        // The button is usable again the moment the recording stops -- the
        // upload happens underneath.
        say('Sent. Ready for another whenever you are.');
        recorder.stop();
    }

    function upload(blob) {
        if (!blob.size) {
            say('That recording was empty. Try again.');
            return;
        }

        var extension = (blob.type.indexOf('mp4') !== -1) ? 'm4a'
            : (blob.type.indexOf('ogg') !== -1) ? 'ogg' : 'webm';

        var data = new FormData();
        data.append('audio', blob, 'memo.' + extension);

        inFlight += 1;
        showQueue();

        fetch(form.action, {method: 'POST', body: data})
            .then(function (response) {
                if (!response.ok) throw new Error('upload rejected');
                startPolling();
            })
            .catch(function () {
                inFlight = Math.max(0, inFlight - 1);
                showQueue();
                say('That one did not upload. Check your connection and try again.');
            });
    }

    // --- keeping the page in step with the server ------------------------
    //
    // Two things make this harder than a plain setInterval. Phones throttle or
    // suspend background timers the moment the screen locks, and returning to
    // the page may restore a frozen snapshot from the back/forward cache. So
    // the interval is only the slow path: the real trigger is the user coming
    // back, which we catch with visibilitychange and pageshow.
    var BASE_DELAY = 2500;
    var MAX_DELAY = 30000;
    var delay = BASE_DELAY;
    var sawWork = false;          // has anything been queued this page view?
    var checking = false;

    function scheduleNext() {
        clearTimeout(poller);
        if (document.hidden) return;      // no point burning battery in the background
        poller = setTimeout(check, delay);
    }

    function startPolling() {
        sawWork = true;
        delay = BASE_DELAY;
        scheduleNext();
    }

    // One status check. Never leaves the page unable to poll again: a failure
    // backs off and retries rather than switching polling off for good.
    function check(immediate) {
        if (checking) return;
        checking = true;
        clearTimeout(poller);

        fetch(form.dataset.statusUrl, {headers: {'Accept': 'application/json'},
                                       cache: 'no-store'})
            .then(function (r) {
                if (!r.ok) throw new Error('status ' + r.status);
                return r.json();
            })
            .then(function (state) {
                checking = false;
                delay = BASE_DELAY;
                inFlight = state.working;
                showQueue();

                if (state.working > 0) {
                    sawWork = true;
                    scheduleNext();
                    return;
                }
                // Queue drained. Reload so the new drafts appear -- but only if
                // something was actually queued, and never mid-recording.
                if (sawWork && !(recorder && recorder.state === 'recording')) {
                    window.location.reload();
                }
            })
            .catch(function () {
                checking = false;
                delay = Math.min(delay * 2, MAX_DELAY);
                scheduleNext();
            });
    }

    // The user coming back is the signal that matters: check straight away
    // rather than waiting for a timer the phone may have frozen.
    document.addEventListener('visibilitychange', function () {
        if (document.hidden) {
            clearTimeout(poller);
        } else if (sawWork) {
            delay = BASE_DELAY;
            check();
        }
    });

    // Fires on a normal load and on a back/forward-cache restore, where the
    // page is a frozen snapshot and every timer missed its turn.
    window.addEventListener('pageshow', function (event) {
        if (event.persisted && sawWork) {
            delay = BASE_DELAY;
            check();
        }
    });

    button.addEventListener('click', function () {
        if (recorder && recorder.state === 'recording') {
            stop();
        } else {
            start();
        }
    });

    // A memo queued before this page loaded (or on another device) is still
    // being written up -- pick the loop back up.
    if (form.dataset.working && parseInt(form.dataset.working, 10) > 0) {
        inFlight = parseInt(form.dataset.working, 10);
        showQueue();
        startPolling();
    }
});
