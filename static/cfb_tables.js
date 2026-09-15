(function () {
    "use strict";

    var mobile = window.matchMedia("(max-width: 767px)");

    function setup(nav) {
        // A page can opt into running these page-level tabs at every width,
        // not just below the phone/tablet breakpoint -- see cfb_game.html,
        // where a full matchup report is long enough that desktop wants the
        // same one-section-at-a-time view mobile already had.
        var always = nav.dataset.mobilePageTabs === "always";
        var active = function () { return always || mobile.matches; };
        var buttons = Array.prototype.slice.call(nav.querySelectorAll("[data-mobile-tab]"));
        var panels = Array.prototype.slice.call(document.querySelectorAll("[data-mobile-tab-panel]"));
        if (!buttons.length || !panels.length) return;

        buttons.forEach(function (button) {
            var name = button.dataset.mobileTab;
            button.id = "mobile-tab-" + name;
            var controlled = [];
            panels.forEach(function (panel, index) {
                if (panel.dataset.mobileTabPanel !== name) return;
                panel.id = "mobile-panel-" + name + "-" + index;
                panel.setAttribute("role", "tabpanel");
                panel.setAttribute("aria-labelledby", button.id);
                controlled.push(panel.id);
            });
            button.setAttribute("aria-controls", controlled.join(" "));
        });

        function known(name) {
            return buttons.some(function (button) { return button.dataset.mobileTab === name; });
        }

        function selectedFromHash() {
            var match = window.location.hash.match(/^#tab-([a-z0-9_-]+)$/);
            if (match && known(match[1])) return match[1];
            try {
                var stored = window.sessionStorage.getItem(
                    "cfb-page-tab:" + window.location.pathname
                );
                if (stored && known(stored)) return stored;
            } catch (error) {}
            return buttons[0].dataset.mobileTab;
        }

        function select(name, moveFocus, updateHash) {
            if (!known(name)) name = buttons[0].dataset.mobileTab;
            buttons.forEach(function (button) {
                var active = button.dataset.mobileTab === name;
                button.setAttribute("aria-selected", active ? "true" : "false");
                button.tabIndex = active ? 0 : -1;
                if (active && moveFocus) button.focus({ preventScroll: true });
            });
            panels.forEach(function (panel) {
                panel.hidden = active() && panel.dataset.mobileTabPanel !== name;
            });
            try {
                window.sessionStorage.setItem("cfb-page-tab:" + window.location.pathname, name);
            } catch (error) {}
            if (updateHash && window.history && window.history.replaceState) {
                window.history.replaceState(null, "", "#tab-" + name);
            }
            // The "on this page" nav built further down this file lists every
            // .section heading regardless of which tab owns it -- it needs to
            // know a tab switch just changed which sections are actually on
            // screen.
            document.dispatchEvent(new CustomEvent("cfb:tabschanged"));
        }

        function applyMode() {
            nav.hidden = !active();
            if (active()) {
                select(selectedFromHash(), false, false);
            } else {
                panels.forEach(function (panel) { panel.hidden = false; });
                document.dispatchEvent(new CustomEvent("cfb:tabschanged"));
            }
        }

        buttons.forEach(function (button, index) {
            button.addEventListener("click", function () {
                select(button.dataset.mobileTab, false, true);
                nav.scrollIntoView({ behavior: "smooth", block: "start" });
            });
            button.addEventListener("keydown", function (event) {
                if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
                event.preventDefault();
                var direction = event.key === "ArrowRight" ? 1 : -1;
                var next = (index + direction + buttons.length) % buttons.length;
                select(buttons[next].dataset.mobileTab, true, true);
            });
        });
        window.addEventListener("hashchange", function () {
            if (active()) select(selectedFromHash(), false, false);
        });
        if (mobile.addEventListener) mobile.addEventListener("change", applyMode);
        else mobile.addListener(applyMode);
        applyMode();
    }

    document.querySelectorAll("[data-mobile-page-tabs]").forEach(setup);
}());

(function () {
    "use strict";

    /* The canonical feed can call the late-August opener Week 1 even when the
       football calendar treats it as Week 0.  Games-to-Watch carries the
       normalized display week, so keep the matchup-section heading aligned
       with the slate the reader is actually seeing. */
    var firstWatchLabel = document.querySelector(".watch-card-top span:first-child");
    if (!firstWatchLabel) return;
    var match = firstWatchLabel.textContent.match(/Week\s+(\d+)/i);
    if (!match) return;
    Array.prototype.forEach.call(document.querySelectorAll(".section-title h2"), function (heading) {
        if (!/^Upcoming Week\b/i.test(heading.textContent.trim())) return;
        heading.textContent = heading.textContent.replace(
            /Upcoming Week(?:\s+\d+)?/i, "Upcoming Week " + match[1]
        );
    });
}());

(function () {
    "use strict";

    /* Long intelligence pages should behave like workspaces, not documents.
       Build one in-page navigator from the headings that already define the
       page so team, matchup, player and history pages stay in sync automatically. */
    var sections = Array.prototype.slice.call(document.querySelectorAll("main .section")).map(
        function (node) {
            var heading = node.querySelector(":scope > h2, :scope > .section-title h2");
            return heading ? { node: node, heading: heading } : null;
        }
    ).filter(Boolean);

    if (sections.length < 3) return;

    // The layout loads cfb_section_nav.css itself, with a version stamped from
    // the file's contents. Injecting a second <link> here duplicated the
    // request, and its hand-written ?v= would have pinned a stale copy for a
    // year now that stamped assets are served immutable.

    function slug(text) {
        return text.toLowerCase()
            .replace(/&/g, " and ")
            .replace(/[^a-z0-9]+/g, "-")
            .replace(/^-+|-+$/g, "") || "section";
    }

    var used = {};
    sections.forEach(function (item) {
        var base = slug(item.heading.textContent.trim());
        var count = used[base] || 0;
        used[base] = count + 1;
        item.id = count ? base + "-" + (count + 1) : base;
        item.node.id = item.node.id || item.id;
        item.node.dataset.sectionNavTarget = "true";
    });

    var nav = document.createElement("nav");
    nav.className = "section-nav";
    nav.setAttribute("aria-label", "On this page");
    nav.innerHTML =
        '<div class="section-nav-inner">' +
          '<span class="section-nav-label">On this page</span>' +
          '<div class="section-nav-links"></div>' +
          '<div class="section-nav-actions">' +
            '<button type="button" data-section-prev title="Previous section" aria-label="Previous section">↑</button>' +
            '<button type="button" data-section-next title="Next section" aria-label="Next section">↓</button>' +
            '<button type="button" data-section-top title="Back to top" aria-label="Back to top">Top</button>' +
          '</div>' +
        '</div>';

    var header = document.querySelector(".site-header");
    if (header) header.insertAdjacentElement("afterend", nav);
    else document.body.insertAdjacentElement("afterbegin", nav);

    var linksHost = nav.querySelector(".section-nav-links");
    sections.forEach(function (item, index) {
        var link = document.createElement("a");
        link.href = "#" + item.node.id;
        link.textContent = item.heading.textContent.trim();
        link.dataset.sectionIndex = String(index);
        link.addEventListener("click", function (event) {
            event.preventDefault();
            item.node.scrollIntoView({ behavior: "smooth", block: "start" });
            if (window.history && window.history.replaceState) {
                window.history.replaceState(null, "", "#" + item.node.id);
            }
        });
        linksHost.appendChild(link);
        item.link = link;
    });

    var previous = nav.querySelector("[data-section-prev]");
    var next = nav.querySelector("[data-section-next]");
    var top = nav.querySelector("[data-section-top]");
    var active = 0;

    // A page with "always" tabs (team, matchup -- see the mobile-page-tabs
    // block above) hides every .section outside the open tab, even on
    // desktop. Offering or scroll-spying a section a reader cannot currently
    // see is exactly what "breaks" this nav on those pages, so every method
    // below works over the currently visible subset, recomputed on demand
    // rather than assumed to be the full list built at parse time.
    //
    // Some panels (the "Middle of the field" split, wired in by
    // passing_matchup_splits) render their own nested .section inside a
    // [data-mobile-tab-panel] wrapper rather than carrying that attribute
    // themselves, so only the wrapper's `hidden` gets toggled -- checking the
    // section's own `hidden` missed that and left it "visible" on every tab.
    // offsetParent is null once any ancestor (or the node itself) is
    // display:none, so it catches both cases without special-casing the wrapper.
    function visibleSections() {
        return sections.filter(function (item) { return item.node.offsetParent !== null; });
    }

    function select(index, move) {
        var shown = visibleSections();
        if (!shown.length) return;
        index = Math.max(0, Math.min(shown.length - 1, index));
        active = index;
        shown.forEach(function (item, position) {
            if (position === index) item.link.setAttribute("aria-current", "location");
            else item.link.removeAttribute("aria-current");
        });
        previous.disabled = index === 0;
        next.disabled = index === shown.length - 1;
        shown[index].link.scrollIntoView({ block: "nearest", inline: "nearest" });
        if (move) shown[index].node.scrollIntoView({ behavior: "smooth", block: "start" });
    }

    function currentSection() {
        var shown = visibleSections();
        if (!shown.length) return;
        var threshold = nav.getBoundingClientRect().bottom + 18;
        var index = 0;
        shown.forEach(function (item, position) {
            if (item.node.getBoundingClientRect().top <= threshold) index = position;
        });
        select(index, false);
    }

    function updateHeight() {
        document.documentElement.style.setProperty("--cfb-section-nav-h", nav.offsetHeight + "px");
    }

    function refresh() {
        var shown = visibleSections();
        sections.forEach(function (item) { item.link.hidden = item.node.offsetParent === null; });
        // cfb_section_nav.css forces `display: block` on this element at
        // >= 980px with normal author-origin priority, which beats the `hidden`
        // attribute's user-agent-stylesheet default outright regardless of
        // specificity -- so hiding it here has to win the same way, with an
        // inline style, not the `hidden` IDL property.
        nav.style.display = shown.length < 2 ? "none" : "";
        updateHeight();
        currentSection();
    }

    previous.addEventListener("click", function () { select(active - 1, true); });
    next.addEventListener("click", function () { select(active + 1, true); });
    top.addEventListener("click", function () {
        window.scrollTo({ top: 0, behavior: "smooth" });
        if (window.history && window.history.replaceState) {
            window.history.replaceState(null, "", window.location.pathname + window.location.search);
        }
    });

    var scrollQueued = false;
    window.addEventListener("scroll", function () {
        if (scrollQueued) return;
        scrollQueued = true;
        window.requestAnimationFrame(function () {
            currentSection();
            scrollQueued = false;
        });
    }, { passive: true });
    var resizeQueued = false;
    window.addEventListener("resize", function () {
        if (resizeQueued) return;
        resizeQueued = true;
        window.requestAnimationFrame(function () {
            updateHeight();
            resizeQueued = false;
        });
    }, { passive: true });
    document.addEventListener("cfb:tabschanged", refresh);

    refresh();
}());

/* A printed box score has to be the whole box score. The tail of a long
   category sits in a closed <details>, and a closed one hides its content
   through the UA's own slot, which no print stylesheet can reach -- so open
   them for the duration of the print and put them back afterwards. */
(function () {
    var reopened = [];
    function expand() {
        reopened = Array.prototype.filter.call(
            document.querySelectorAll("details.table-overflow"),
            function (node) { return !node.open; });
        reopened.forEach(function (node) { node.open = true; });
    }
    function restore() {
        reopened.forEach(function (node) { node.open = false; });
        reopened = [];
    }
    window.addEventListener("beforeprint", expand);
    window.addEventListener("afterprint", restore);
})();
