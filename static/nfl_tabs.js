(function () {
    "use strict";
    document.querySelectorAll("[data-nfl-tabs]").forEach(function (nav, navIndex) {
        var buttons = Array.prototype.slice.call(nav.querySelectorAll("[data-nfl-tab]"));
        var panels = Array.prototype.slice.call(document.querySelectorAll("[data-nfl-panel]"));
        if (!buttons.length || !panels.length) return;
        var storageKey = "nfl-page-tab:" + window.location.pathname + ":" + navIndex;
        function select(name, focus, persist, updateHash) {
            if (!buttons.some(function (button) { return button.dataset.nflTab === name; })) {
                name = buttons[0].dataset.nflTab;
            }
            buttons.forEach(function (button) {
                var active = button.dataset.nflTab === name;
                button.setAttribute("aria-selected", active ? "true" : "false");
                button.tabIndex = active ? 0 : -1;
                if (active && focus) button.focus();
            });
            panels.forEach(function (panel) { panel.hidden = panel.dataset.nflPanel !== name; });
            if (persist) {
                try { window.sessionStorage.setItem(storageKey, name); } catch (error) {}
            }
            if (updateHash && window.history && window.history.replaceState) {
                window.history.replaceState(null, "", "#tab-" + name);
                window.requestAnimationFrame(function () {
                    nav.scrollIntoView({ block: "start", behavior: "smooth" });
                });
            }
        }
        buttons.forEach(function (button, index) {
            button.addEventListener("click", function () {
                select(button.dataset.nflTab, false, true, true);
            });
            button.addEventListener("keydown", function (event) {
                if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                event.preventDefault();
                var next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
                    (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
                select(buttons[next].dataset.nflTab, true, true, true);
            });
        });
        function tabFromHash() {
            var hash = window.location.hash.replace(/^#/, "");
            if (!hash) return null;
            if (hash.indexOf("tab-") === 0) return hash.slice(4);
            var target = document.getElementById(hash);
            return target && target.dataset ? target.dataset.nflPanel : null;
        }
        var selected;
        try { selected = window.sessionStorage.getItem(storageKey); } catch (error) {}
        select(tabFromHash() || selected || buttons[0].dataset.nflTab, false, false, false);
        window.addEventListener("hashchange", function () {
            var fromHash = tabFromHash();
            if (fromHash) select(fromHash, false, true, false);
        });
    });
    document.querySelectorAll("[data-nfl-copy-report]").forEach(function (button) {
        button.addEventListener("click", function () {
            var url = window.location.origin + window.location.pathname + window.location.search + "#tab-review";
            function done() {
                var label = button.textContent;
                button.textContent = "Copied";
                window.setTimeout(function () { button.textContent = label; }, 1400);
            }
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(url).then(done).catch(function () {
                    window.prompt("Copy report link", url);
                });
            } else {
                window.prompt("Copy report link", url);
            }
        });
    });
    document.querySelectorAll("[data-nfl-print-report]").forEach(function (button) {
        button.addEventListener("click", function () { window.print(); });
    });
    if (window.matchMedia && window.matchMedia("(max-width: 680px)").matches) {
        document.querySelectorAll(".depth-unit").forEach(function (unit) {
            Array.prototype.slice.call(unit.querySelectorAll(".position-room"), 1)
                .forEach(function (room) { room.removeAttribute("open"); });
        });
    }
}());
