(function () {
    "use strict";

    // .injury-tooltip and .pass-zone-tooltip are CSS :hover/:focus-within
    // popups, positioned `absolute` relative to their trigger. Every one of
    // them lives inside a `.section` (or a `.pass-matchup-card`), and both
    // clip with `overflow:hidden`/`overflow-x:auto` for their own rounded-
    // corner and horizontal-scroll styling -- so a tooltip wide enough to
    // reach that edge gets silently clipped no matter its z-index. z-index
    // only orders paint among unclipped elements; it cannot undo an
    // ancestor's overflow clipping. The fix is `position: fixed`, which
    // escapes every scrolling/clipping ancestor by being positioned against
    // the viewport instead -- but a fixed element needs real coordinates,
    // which only JS can compute from the trigger's actual on-screen position.
    var SELECTOR = ".injury-tooltip, .pass-zone-tooltip";
    var MARGIN = 10;
    var active = null;
    var hideTimer = null;

    // The tooltip now renders wherever place() puts it on screen, not
    // necessarily touching the trigger -- so leaving the trigger to move
    // toward the tooltip (to click a player link inside it) would fire
    // mouseleave before the cursor ever reaches it. Debounce the hide and
    // cancel it if the pointer lands on either element.
    function cancelHide() {
        if (hideTimer !== null) { clearTimeout(hideTimer); hideTimer = null; }
    }

    function scheduleHide() {
        cancelHide();
        hideTimer = setTimeout(hide, 150);
    }

    function place(trigger, tooltip) {
        var triggerRect = trigger.getBoundingClientRect();
        var tipRect = tooltip.getBoundingClientRect();
        var left = triggerRect.left;
        var top = triggerRect.bottom + 8;
        if (left + tipRect.width > window.innerWidth - MARGIN) {
            left = window.innerWidth - tipRect.width - MARGIN;
        }
        if (left < MARGIN) left = MARGIN;
        if (top + tipRect.height > window.innerHeight - MARGIN) {
            top = triggerRect.top - tipRect.height - 8;
        }
        if (top < MARGIN) top = MARGIN;
        var style = tooltip.style;
        style.setProperty("position", "fixed", "important");
        style.setProperty("left", left + "px", "important");
        style.setProperty("top", top + "px", "important");
        style.setProperty("right", "auto", "important");
        style.setProperty("bottom", "auto", "important");
        style.setProperty("z-index", "999", "important");
    }

    function findTooltip(trigger) {
        for (var i = 0; i < trigger.children.length; i++) {
            if (trigger.children[i].matches(SELECTOR)) return trigger.children[i];
        }
        return null;
    }

    function show(trigger) {
        var tooltip = findTooltip(trigger);
        if (!tooltip) return;
        if (active && active.tooltip !== tooltip) hide();
        var display = tooltip.classList.contains("injury-tooltip") ? "flex" : "block";
        tooltip.style.setProperty("display", display, "important");
        active = { trigger: trigger, tooltip: tooltip };
        place(trigger, tooltip);
    }

    function hide() {
        if (!active) return;
        var style = active.tooltip.style;
        ["display", "position", "left", "top", "right", "bottom", "z-index"].forEach(function (prop) {
            style.removeProperty(prop);
        });
        active = null;
    }

    document.querySelectorAll(SELECTOR).forEach(function (tooltip) {
        var trigger = tooltip.parentElement;
        if (!trigger || trigger.dataset.tooltipBound) return;
        trigger.dataset.tooltipBound = "true";
        trigger.addEventListener("mouseenter", function () { cancelHide(); show(trigger); });
        trigger.addEventListener("mouseleave", scheduleHide);
        tooltip.addEventListener("mouseenter", cancelHide);
        tooltip.addEventListener("mouseleave", scheduleHide);
        trigger.addEventListener("focusin", function () { cancelHide(); show(trigger); });
        trigger.addEventListener("focusout", function (event) {
            if (active && active.trigger === trigger && !trigger.contains(event.relatedTarget) &&
                    !tooltip.contains(event.relatedTarget)) hide();
        });
    });

    window.addEventListener("scroll", function () {
        if (active) place(active.trigger, active.tooltip);
    }, true);
    window.addEventListener("resize", function () {
        if (active) place(active.trigger, active.tooltip);
    });
}());
