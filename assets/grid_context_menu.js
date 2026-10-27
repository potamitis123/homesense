/*
 * Right-click menu for the Audio Events grid.
 *
 * The menu itself already exists in the layout (#context-menu, with the
 * ctx-download and ctx-delete items) and both of its Dash callbacks are
 * wired. What was missing was anything to make it appear: AG Grid's own
 * context menu is an Enterprise feature, and this project runs Community,
 * so right-clicking a row did nothing and the header's "Right-click for
 * options" was a promise with no implementation behind it.
 *
 * This file supplies only the showing and hiding. It deliberately performs
 * no action of its own -- clicking an item just registers n_clicks on the
 * existing ListGroupItem, so the Python callbacks stay the single place
 * where anything actually happens.
 */
(function () {
    "use strict";

    var GRID_ID = "audio_grid_table";
    var MENU_ID = "context-menu";

    function menu() {
        return document.getElementById(MENU_ID);
    }

    function hide() {
        var m = menu();
        if (m) m.style.display = "none";
    }

    function show(x, y) {
        var m = menu();
        if (!m) return;

        // Rendered off-screen first so its real height can be measured; a menu
        // opened near the bottom of the window must flip upwards rather than
        // extend past the viewport where it cannot be clicked.
        m.style.visibility = "hidden";
        m.style.display = "block";
        var w = m.offsetWidth || 200;
        var h = m.offsetHeight || 80;

        var left = Math.min(x, window.innerWidth - w - 8);
        var top = Math.min(y, window.innerHeight - h - 8);
        m.style.left = Math.max(8, left) + "px";
        m.style.top = Math.max(8, top) + "px";
        m.style.visibility = "visible";
    }

    /*
     * Both callbacks act on the grid's selectedRows, so a right-click on a row
     * that is not selected has to select it first -- otherwise the menu opens
     * and every item silently does nothing, which is worse than no menu.
     * A right-click inside an existing multi-row selection leaves that
     * selection alone, so "delete these twelve" still works.
     */
    function selectRowUnderCursor(rowEl) {
        try {
            var api = window.dash_ag_grid && window.dash_ag_grid.getApi
                ? window.dash_ag_grid.getApi(GRID_ID) : null;
            if (!api) return;
            var idx = parseInt(rowEl.getAttribute("row-index"), 10);
            if (isNaN(idx)) return;
            var node = api.getDisplayedRowAtIndex(idx);
            if (node && !node.isSelected()) {
                api.deselectAll();
                node.setSelected(true);
            }
        } catch (err) {
            /* Selection is a convenience; never let it stop the menu opening. */
        }
    }

    document.addEventListener("contextmenu", function (e) {
        var grid = document.getElementById(GRID_ID);
        if (!grid || !grid.contains(e.target)) return;          // not our grid

        var rowEl = e.target.closest(".ag-row");
        if (!rowEl) return;                                     // header, gap
        if (e.target.closest(".ag-header")) return;

        e.preventDefault();                                     // no browser menu
        selectRowUnderCursor(rowEl);
        show(e.clientX, e.clientY);
    });

    // Any ordinary click dismisses it, including a click on one of its own
    // items: the item's own handler has already run by then, so the menu
    // closes as soon as an action is chosen.
    document.addEventListener("click", hide);
    document.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
    window.addEventListener("blur", hide);
    document.addEventListener("keydown", function (e) {
        if (e.key === "Escape") hide();
    });
})();
