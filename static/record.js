// Voice memo recording.
//
// The browser only captures audio -- transcription happens server-side, so this
// works in every browser with MediaRecorder, not just the ones with a speech
// API. getUserMedia needs a secure context: fine on localhost, needs HTTPS once
// deployed.
//
// Uploads are fire-and-forget. The server stores the audio, queues it, and
// answers 202 immediately, so stopping a recording is instant. You can start
// another one straight away, or close the tab -- the work carries on without
// the browser, and the drafts are waiting at next login.
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

    function showQueue() {
        if (!queue) return;
        if (inFlight > 0) {
            queue.hidden = false;
            queue.textContent = inFlight === 1
                ? 'Working on 1 memo in the background. You can keep going or close this page.'
                : 'Working on ' + inFlight + ' memos in the background. You can keep going or close this page.';
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

    // Ask the server how many memos are still being worked on. When the queue
    // empties, reload so the new drafts appear. Purely a convenience -- the
    // drafts are saved whether or not this page is still open.
    function startPolling() {
        if (poller) return;
        poller = setInterval(function () {
            fetch(form.dataset.statusUrl, {headers: {'Accept': 'application/json'}})
                .then(function (r) { return r.json(); })
                .then(function (state) {
                    inFlight = state.working;
                    showQueue();
                    if (state.working === 0) {
                        clearInterval(poller);
                        poller = null;
                        if (recorder && recorder.state === 'recording') return;
                        window.location.reload();
                    }
                })
                .catch(function () {
                    clearInterval(poller);
                    poller = null;
                });
        }, 2500);
    }

    button.addEventListener('click', function () {
        if (recorder && recorder.state === 'recording') {
            stop();
        } else {
            start();
        }
    });

    // A memo queued before this page loaded (or by another device) is still
    // being worked on -- pick the polling back up.
    if (form.dataset.working && parseInt(form.dataset.working, 10) > 0) {
        inFlight = parseInt(form.dataset.working, 10);
        showQueue();
        startPolling();
    }
});
