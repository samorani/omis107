// Voice memo recording.
//
// The browser only captures audio here -- transcription happens server-side, so
// this works in every browser that has MediaRecorder, not just the ones with a
// speech API. Note that getUserMedia requires a secure context: it works on
// localhost, and will need HTTPS once this is deployed.
document.addEventListener('DOMContentLoaded', function () {
    var button = document.getElementById('record-btn');
    if (!button) return;

    var status = document.getElementById('record-status');
    var form = document.getElementById('record-form');
    var supported = !!(navigator.mediaDevices && window.MediaRecorder);

    if (!supported) {
        button.disabled = true;
        say('This browser cannot record audio. Try Chrome, Edge, or Safari.');
        return;
    }

    var recorder = null;
    var chunks = [];
    var timer = null;
    var startedAt = 0;

    function say(message) {
        if (status) status.textContent = message;
    }

    function pickMimeType() {
        // Chrome and Firefox give webm; Safari gives mp4. The transcription API
        // accepts both, so take whichever the browser actually supports.
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
        button.querySelector('.record-label').textContent = 'Stop';
    }

    function stop() {
        clearInterval(timer);
        button.classList.remove('is-recording');
        button.disabled = true;
        button.querySelector('.record-label').textContent = 'Working...';
        say('Sending it over. This takes a few seconds.');
        recorder.stop();
    }

    function upload(blob) {
        if (!blob.size) {
            say('That recording was empty. Try again.');
            reset();
            return;
        }

        var extension = (blob.type.indexOf('mp4') !== -1) ? 'm4a'
            : (blob.type.indexOf('ogg') !== -1) ? 'ogg' : 'webm';

        var data = new FormData();
        data.append('audio', blob, 'memo.' + extension);

        fetch(form.action, {method: 'POST', body: data})
            .then(function (response) {
                // The server answers with a redirect back to the project page,
                // where the new drafts and any message are rendered.
                window.location.href = response.url || window.location.href;
            })
            .catch(function () {
                say('Upload failed. Check your connection and try again.');
                reset();
            });
    }

    function reset() {
        button.disabled = false;
        button.classList.remove('is-recording');
        button.querySelector('.record-label').textContent = 'Record a memo';
    }

    button.addEventListener('click', function () {
        if (recorder && recorder.state === 'recording') {
            stop();
        } else {
            start();
        }
    });
});
