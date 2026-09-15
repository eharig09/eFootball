(function () {
    "use strict";

    // Click-to-sort for any `<th data-sortable="true">` in the shared
    // data_table macro (templates/_tables.html) — shared by the CFB and NFL
    // page families, since every statistical table in the app renders
    // through that one macro.

    function numberValue(value) {
        var normalized = value.replace(/,/g, "").replace(/%$/, "").trim();
        var number = Number(normalized);
        return Number.isFinite(number) ? number : null;
    }

    function sortTable(header) {
        var table = header.closest("table");
        var body = table && table.tBodies[0];
        if (!body || body.rows.length < 2) return;
        var headers = Array.prototype.slice.call(header.parentElement.cells);
        var column = headers.indexOf(header);
        var current = header.getAttribute("aria-sort");
        var numeric = header.dataset.sortKind === "number";
        var descending = current === "ascending" || (current === "none" && numeric);
        headers.forEach(function (item) { item.setAttribute("aria-sort", "none"); });
        header.setAttribute("aria-sort", descending ? "descending" : "ascending");
        var rows = Array.prototype.map.call(body.rows, function (row, index) {
            var cell = row.cells[column];
            var raw = (cell && cell.dataset.sortValue || cell && cell.textContent || "").trim();
            return { row: row, index: index, raw: raw,
                     value: numeric ? numberValue(raw) : raw.toLocaleLowerCase() };
        });
        rows.sort(function (left, right) {
            var leftMissing = left.value === null || left.value === "" || left.raw === "—";
            var rightMissing = right.value === null || right.value === "" || right.raw === "—";
            if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
            if (left.value === right.value) return left.index - right.index;
            var order = left.value < right.value ? -1 : 1;
            return descending ? -order : order;
        });
        rows.forEach(function (item) { body.appendChild(item.row); });
    }

    document.addEventListener("click", function (event) {
        var header = event.target.closest("th[data-sortable='true']");
        if (header) sortTable(header);
    });
    document.addEventListener("keydown", function (event) {
        if (event.key !== "Enter" && event.key !== " ") return;
        var header = event.target.closest("th[data-sortable='true']");
        if (!header) return;
        event.preventDefault();
        sortTable(header);
    });
}());
