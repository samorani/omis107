// Adds show/hide behaviour to every .toggle-password button on the page.
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.toggle-password').forEach(function (button) {
        var input = document.getElementById(button.dataset.target);
        if (!input) return;

        var eye = button.querySelector('.icon-eye');
        var eyeOff = button.querySelector('.icon-eye-off');

        button.addEventListener('click', function () {
            var show = input.type === 'password';
            input.type = show ? 'text' : 'password';
            eye.hidden = show;
            eyeOff.hidden = !show;
            button.setAttribute('aria-pressed', show ? 'true' : 'false');
            button.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
            input.focus();
        });
    });
});
