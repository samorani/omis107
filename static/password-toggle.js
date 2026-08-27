// Adds a Show/Hide toggle to every .password-field on the page.
// Purely client-side: the value never leaves the input, and the field keeps
// its name and value, so the form submits exactly as it would without JS.
document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll(".toggle-password").forEach(function (button) {
        var input = document.getElementById(button.dataset.target);
        if (!input) {
            return;
        }

        // Only reveal the button once we know scripting works.
        button.hidden = false;

        button.addEventListener("click", function () {
            var reveal = input.type === "password";
            input.type = reveal ? "text" : "password";
            button.textContent = reveal ? "Hide" : "Show";
            button.setAttribute("aria-label", (reveal ? "Hide" : "Show") + " password");
            button.setAttribute("aria-pressed", String(reveal));

            // Return focus to the field with the caret at the end, so the user
            // can keep typing instead of hunting for their place.
            input.focus();
            try {
                input.setSelectionRange(input.value.length, input.value.length);
            } catch (e) {
                // Some browsers refuse setSelectionRange on password inputs.
            }
        });
    });
});
