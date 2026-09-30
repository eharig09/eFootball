(function () {
    "use strict";
    // Filters the play-by-play table by kind. Without JavaScript every play stays visible.
    document.querySelectorAll("[data-play-filter]").forEach(function (root) {
        var buttons = Array.prototype.slice.call(root.querySelectorAll("[data-play-kind]"));
        var rows = Array.prototype.slice.call(root.querySelectorAll("tbody tr[data-kinds]"));
        var count = root.querySelector("[data-play-count]");
        function apply(kind) {
            var shown = 0;
            rows.forEach(function (row) {
                var match = kind === "all" || row.dataset.kinds.split(" ").indexOf(kind) !== -1;
                row.hidden = !match;
                if (match) shown += 1;
            });
            buttons.forEach(function (button) {
                button.classList.toggle("is-active", button.dataset.playKind === kind);
                button.setAttribute("aria-pressed", button.dataset.playKind === kind ? "true" : "false");
            });
            if (count) count.textContent = shown + (shown === 1 ? " play" : " plays");
        }
        buttons.forEach(function (button) {
            button.addEventListener("click", function () { apply(button.dataset.playKind); });
        });
        var initial = buttons.filter(function (button) { return button.classList.contains("is-active"); })[0];
        apply(initial ? initial.dataset.playKind : "all");
    });
}());
