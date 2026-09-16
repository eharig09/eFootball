(function () {
    "use strict";

    // .injury-tooltip and .pass-zone-tooltip are CSS :hover/:focus-within
    // popups, positioned `absolute` relative to their trigger. Every one of
    // them lives inside a `.section` (or a `.pass-matchup-card`), and both
    // clip with `overflow:hidden`/`overflow-x:auto` for their own rounded-
    // corner and horizontal-scroll styling -- so a tooltip wide enough to
    // reach that edge gets silently clipped no matter its z-index. z-index
    // only orders paint among unclipped elements; it cannot undo an
    // ancestor's overflow clipping. `position: fixed` escapes the clipping,
    // but NOT an ancestor's z-index stacking context: a dense grid cell
    // that also gets `z-index` on hover/focus (to paint over its neighbors)
    // becomes a stacking context itself, and a fixed-position descendant is
    // still ordered inside THAT context, not the page's root one -- so a
    // tooltip could still paint behind some other, later-in-the-DOM
    // hovered/focused element's own stacking context, even when sized and
    // positioned correctly. The only way to guarantee it always paints
    // above everything is to move it out of the tree entirely, onto
    // <body>, for as long as it's shown (the standard "portal" pattern),
    // then move it back to its original spot on hide -- both so the
    // server-rendered structure stays intact between shows, and so a
    // trigger's own child-lookup keeps working the next time it's opened.
    var SELECTOR = ".injury-tooltip, .pass-zone-tooltip";
    var MARGIN = 10;
    var active = null;
    var hideTimer = null;
    //: tooltip -> {trigger, parent, next}, captured once at bind time.
    var homes = new WeakMap();
    //: trigger -> tooltip, captured once at bind time so a tooltip currently
    //: moved onto <body> (re-hovering the cell that's already open) can
    //: still be found without depending on its current DOM position.
    var tooltipByTrigger = new WeakMap();

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
        // The base (non-JS) CSS still carries `left:50%;transform:translateX(-50%)`
        // from when a tooltip centered itself under its trigger without any
        // JS involvement. That transform survives onto the portaled element
        // and silently shifts it left by half its own width on top of the
        // `left` computed below -- harmless on a wide desktop viewport where
        // there's room to spare, but on a narrow phone screen a few hundred
        // pixels of unwanted shift reliably pushes the tooltip off the left
        // edge. Clearing it here is what actually makes `left`/`top` mean
        // what the math below assumes they mean.
        var style = tooltip.style;
        style.setProperty("transform", "none", "important");
        style.setProperty("max-height", "none", "important");
        style.removeProperty("overflow-y");
        var triggerRect = trigger.getBoundingClientRect();
        var tipRect = tooltip.getBoundingClientRect();
        var left = triggerRect.left;
        if (left + tipRect.width > window.innerWidth - MARGIN) {
            left = window.innerWidth - tipRect.width - MARGIN;
        }
        if (left < MARGIN) left = MARGIN;
        // Prefer whichever side of the trigger has more room, then clamp the
        // tooltip's own height to whatever that side actually has -- a
        // contributor-heavy tooltip (8 offense rows + a position-grouped
        // defense breakdown) can easily be taller than a short phone
        // viewport, and without this it just runs off the bottom (or top)
        // with no way to reach the rest of it.
        var spaceBelow = window.innerHeight - triggerRect.bottom - 8 - MARGIN;
        var spaceAbove = triggerRect.top - 8 - MARGIN;
        var top, maxHeight;
        if (tipRect.height <= spaceBelow || spaceBelow >= spaceAbove) {
            top = triggerRect.bottom + 8;
            maxHeight = spaceBelow;
        } else {
            maxHeight = spaceAbove;
            top = triggerRect.top - Math.min(tipRect.height, maxHeight) - 8;
        }
        if (top < MARGIN) top = MARGIN;
        style.setProperty("position", "fixed", "important");
        style.setProperty("left", left + "px", "important");
        style.setProperty("top", top + "px", "important");
        style.setProperty("right", "auto", "important");
        style.setProperty("bottom", "auto", "important");
        style.setProperty("z-index", "99999", "important");
        if (tipRect.height > maxHeight) {
            style.setProperty("max-height", Math.max(160, maxHeight) + "px", "important");
            style.setProperty("overflow-y", "auto", "important");
        }
    }

    function clear(tooltip) {
        var style = tooltip.style;
        ["display", "position", "left", "top", "right", "bottom", "z-index",
         "transform", "max-height", "overflow-y"].forEach(function (prop) {
            style.removeProperty(prop);
        });
        var home = homes.get(tooltip);
        if (home && tooltip.parentNode === document.body) {
            home.parent.insertBefore(tooltip, home.next);
        }
    }

    // Dense grids (zone charts, run direction, situational strips) made a
    // real gap visible: the single `active` pointer only ever tracks what
    // THIS module last opened, so a tooltip left visible by any other path
    // (a stale reference after a fast mouse trail across adjacent cells, a
    // focus event racing a hide timer) never got cleared. Clearing every
    // match in the DOM, not just the tracked one, is the only way to
    // guarantee at most one is ever visible at a time regardless of how
    // state drifted.
    function hideAll(except) {
        document.querySelectorAll(SELECTOR).forEach(function (tooltip) {
            if (tooltip !== except) clear(tooltip);
        });
    }

    function show(trigger) {
        var tooltip = tooltipByTrigger.get(trigger);
        if (!tooltip) return;
        hideAll(tooltip);
        document.body.appendChild(tooltip);
        var display = tooltip.classList.contains("injury-tooltip") ? "flex" : "block";
        tooltip.style.setProperty("display", display, "important");
        active = { trigger: trigger, tooltip: tooltip };
        place(trigger, tooltip);
    }

    function hide() {
        if (!active) return;
        clear(active.tooltip);
        active = null;
    }

    document.querySelectorAll(SELECTOR).forEach(function (tooltip) {
        var trigger = tooltip.parentElement;
        if (!trigger || trigger.dataset.tooltipBound) return;
        trigger.dataset.tooltipBound = "true";
        tooltipByTrigger.set(trigger, tooltip);
        homes.set(tooltip, { trigger: trigger, parent: tooltip.parentNode, next: tooltip.nextSibling });
        trigger.addEventListener("mouseenter", function () { cancelHide(); show(trigger); });
        trigger.addEventListener("mouseleave", scheduleHide);
        tooltip.addEventListener("mouseenter", cancelHide);
        tooltip.addEventListener("mouseleave", scheduleHide);
        trigger.addEventListener("focusin", function () { cancelHide(); show(trigger); });
        trigger.addEventListener("focusout", function (event) {
            if (active && active.trigger === trigger && !trigger.contains(event.relatedTarget) &&
                    !(active.tooltip.contains && active.tooltip.contains(event.relatedTarget))) hide();
        });
    });

    window.addEventListener("scroll", function () {
        if (active) place(active.trigger, active.tooltip);
    }, true);
    window.addEventListener("resize", function () {
        if (active) place(active.trigger, active.tooltip);
    });
}());
