// Feedback for the "Mark done" button.
//
// Ticking something off posts a form and waits for the round trip, which on a
// phone is long enough to wonder whether the tap registered. So the button
// answers immediately: the ring fills, the check draws itself in, and the label
// flips to "Done" while the request is still in the air.
document.addEventListener('DOMContentLoaded', function () {
    document.addEventListener('submit', function (event) {
        var form = event.target;
        var button = form.querySelector ? form.querySelector('.done-btn') : null;
        if (!button) return;

        // A second tap while the first is still going would toggle it back.
        if (form.dataset.submitting === 'yes') {
            event.preventDefault();
            return;
        }
        form.dataset.submitting = 'yes';

        // Only animate the direction that reads as completion. Re-opening a
        // finished reminder keeps the plain look -- a check drawing itself in
        // would say the opposite of what just happened.
        if (!button.classList.contains('is-done')) {
            button.classList.add('is-checking');
            var text = button.querySelector('.done-text');
            if (text) text.textContent = 'Done';
        }
    }, true);
});
