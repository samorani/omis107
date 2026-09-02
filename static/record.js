// Voice memo recording.
//
// The browser only captures audio -- transcription and parsing happen on the
// server, so this works in every browser with MediaRecorder, not just the ones
// with a speech API. getUserMedia needs a secure context: fine on localhost,
// needs HTTPS once deployed.
//
// One memo at a time. The upload request does the whole job and only answers
// once the drafts exist, so the button stays locked until it comes back -- you
// always know whether the last memo landed before starting the next.
document.addEventListener('DOMContentLoaded', function () {
    var button = document.getElementById('record-btn');
    if (!button) return;

    var status = document.getElementById('record-status');
    var queue = document.getElementById('record-queue');
    var form = document.getElementById('record-form');
    var slot = document.getElementById('drafts-slot');
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

    function say(message) {
        if (status) status.textContent = message || '';
    }

    function working(message) {
        if (!queue) return;
        queue.textContent = message || '';
        queue.hidden = !message;
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
        recorder.stop();
    }

    function idle() {
        button.disabled = false;
        button.classList.remove('is-recording');
        label.textContent = 'Record a memo';
        working('');
    }

    function upload(blob) {
        if (!blob.size) {
            say('That recording was empty. Try again.');
            idle();
            return;
        }

        var extension = (blob.type.indexOf('mp4') !== -1) ? 'm4a'
            : (blob.type.indexOf('ogg') !== -1) ? 'ogg' : 'webm';

        var data = new FormData();
        data.append('audio', blob, 'memo.' + extension);

        // Locked until the server answers: one memo at a time.
        button.disabled = true;
        label.textContent = 'Writing it up';
        say('');
        working('Writing it up… this takes a few seconds.');

        fetch(form.action, {method: 'POST', body: data, credentials: 'same-origin'})
            .then(function (response) {
                return response.json()
                    .catch(function () { return {}; })
                    .then(function (body) { return {ok: response.ok, body: body}; });
            })
            .then(function (result) {
                if (!result.ok) throw new Error(result.body.error || 'That one did not go through.');
                return refreshDrafts(result.body.drafts).then(function () {
                    idle();
                    say(result.body.created
                        ? (result.body.created === 1
                            ? '1 draft reminder added below.'
                            : result.body.created + ' draft reminders added below.')
                        : 'Nothing in that one looked like a reminder.');
                });
            })
            .catch(function (err) {
                idle();
                say(err.message || 'That one did not go through. Try again.');
            });
    }

    // Pull the freshly rendered panel and put it in place, so the new drafts
    // appear without a page reload.
    function refreshDrafts(token) {
        if (!slot) return Promise.resolve();
        return fetch(slot.dataset.fragmentUrl, {credentials: 'same-origin', cache: 'no-store'})
            .then(function (r) {
                if (!r.ok) throw new Error('fragment ' + r.status);
                return r.text();
            })
            .then(function (html) {
                slot.innerHTML = html;
                if (token) slot.dataset.token = token;
                slot.classList.add('just-arrived');
                setTimeout(function () { slot.classList.remove('just-arrived'); }, 1200);
            })
            .catch(function () {
                // Could not patch the page; fall back to the blunt instrument.
                window.location.reload();
            });
    }

    button.addEventListener('click', function () {
        if (button.disabled) return;
        if (recorder && recorder.state === 'recording') {
            stop();
        } else {
            start();
        }
    });
});
