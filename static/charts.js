(function () {
    "use strict";
    if (typeof Chart === "undefined") return;

    Chart.defaults.color = "#93a1b3";
    Chart.defaults.borderColor = "#293945";
    Chart.defaults.font.family = '"Cascadia Mono", Consolas, monospace';

    // Mirrors sports_aggregator.tables.format_value's format keys (and CFB's
    // own cell filter), since every chart's `format` field comes straight
    // from that same vocabulary -- one number reads the same way whether
    // it's in a table cell or a chart tick, on either sport's pages.
    var FORMATTERS = {
        int: function (v) { return String(Math.round(v)); },
        big: function (v) { return Math.round(v).toLocaleString(); },
        f1: function (v) { return v.toFixed(1); },
        f2: function (v) { return v.toFixed(2); },
        f3: function (v) { return v.toFixed(3); },
        signed: function (v) { return (v >= 0 ? "+" : "") + Math.round(v); },
        signed2: function (v) { return (v >= 0 ? "+" : "") + v.toFixed(2); },
        rate: function (v) { return (v * 100).toFixed(1) + "%"; },
        pct: function (v) { return v.toFixed(1) + "%"; },
    };

    function format(value, fmt) {
        if (value === null || value === undefined || Number.isNaN(value)) return "—";
        var fn = FORMATTERS[fmt];
        return fn ? fn(value) : String(value);
    }

    function pointKey(point) {
        // week_label already carries a prior-season prefix when a series is
        // backfilled. Other CFB sources intentionally omit game_id/season,
        // so including those here would split one real week into duplicate
        // x-axis slots instead of aligning offense, defense and scoring.
        return point.week_label || [point.season || "", point.week || ""].join("|");
    }

    function mergedSlots(series) {
        var seen = {};
        var slots = [];
        series.forEach(function (chartData) {
            chartData.values.forEach(function (point) {
                var key = pointKey(point);
                if (seen[key]) return;
                seen[key] = true;
                slots.push(point);
            });
        });
        return slots;
    }

    function buildDataset(chartData, axisId, slots) {
        var byPoint = {};
        chartData.values.forEach(function (point) { byPoint[pointKey(point)] = point; });
        var points = slots.map(function (slot) { return byPoint[pointKey(slot)] || null; });
        return {
            label: chartData.label,
            data: points.map(function (point) { return point ? point.value : null; }),
            pointMeta: points,
            borderColor: chartData.color,
            backgroundColor: chartData.color,
            pointBackgroundColor: chartData.color,
            yAxisID: axisId,
            tension: 0.25,
            spanGaps: true,
            pointRadius: 4,
            pointHoverRadius: 6,
            borderWidth: 2.5,
        };
    }

    function axisFor(chartData) {
        return chartData.axis || {
            key: "metric:" + chartData.key,
            label: chartData.label,
            format: chartData.format,
        };
    }

    document.querySelectorAll("[data-chart-workbench]").forEach(function (root) {
        var canvas = root.querySelector("canvas");
        var payload = JSON.parse(root.querySelector("[data-chart-json]").textContent);
        var gameUrlPrefix = root.dataset.gameUrl || "/nfl/games/";
        var byKey = {};
        payload.forEach(function (chartData) { byKey[chartData.key] = chartData; });
        var inputs = Array.prototype.slice.call(root.querySelectorAll("input[data-metric]"));
        var order = inputs.filter(function (input) { return input.checked; });
        if (!order.length) return;

        var chart = new Chart(canvas.getContext("2d"), {
            type: "line",
            data: { labels: [], datasets: [] },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                interaction: { mode: "index", intersect: false },
                onClick: function (event, elements) {
                    if (!elements.length) return;
                    var el = elements[0];
                    var input = order[el.datasetIndex];
                    var chartData = input && byKey[input.dataset.metric];
                    var dataset = chart.data.datasets[el.datasetIndex];
                    var point = dataset && dataset.pointMeta[el.index];
                    if (point && point.game_id) {
                        window.location.href = gameUrlPrefix + point.game_id + "/";
                    }
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                if (!items.length) return "";
                                var dataset = chart.data.datasets[items[0].datasetIndex];
                                var point = dataset && dataset.pointMeta[items[0].dataIndex];
                                if (!point) return "";
                                return "Week " + point.week + (point.opponent ? " vs " + point.opponent : "");
                            },
                            label: function (item) {
                                var input = order[item.datasetIndex];
                                var chartData = input && byKey[input.dataset.metric];
                                return chartData
                                    ? chartData.label + ": " + format(item.parsed.y, chartData.format)
                                    : item.formattedValue;
                            },
                        },
                    },
                },
                scales: {
                    y: {
                        position: "left",
                        ticks: { callback: function (v) { return v; } },
                        title: { display: true, text: "" },
                    },
                    y1: {
                        position: "right",
                        display: false,
                        grid: { drawOnChartArea: false },
                        ticks: { callback: function (v) { return v; } },
                        title: { display: true, text: "" },
                    },
                    x: { grid: { display: false } },
                },
            },
        });

        function sync() {
            var selected = order.map(function (input) { return byKey[input.dataset.metric]; });
            var axes = [];
            selected.forEach(function (chartData) {
                var axis = axisFor(chartData);
                if (!axes.some(function (item) { return item.key === axis.key; })) axes.push(axis);
            });
            var primary = axes[0];
            var secondary = axes[1] || null;
            var slots = mergedSlots(selected);
            chart.data.labels = slots.map(function (point) { return point.week_label; });
            chart.data.datasets = order.map(function (input) {
                var chartData = byKey[input.dataset.metric];
                return buildDataset(chartData, axisFor(chartData).key === primary.key ? "y" : "y1", slots);
            });
            var yScale = chart.options.scales.y;
            yScale.ticks.callback = function (v) { return format(v, primary.format); };
            yScale.title.text = primary.label;
            var y1Scale = chart.options.scales.y1;
            y1Scale.display = !!secondary;
            if (secondary) {
                y1Scale.ticks.callback = function (v) { return format(v, secondary.format); };
                y1Scale.title.text = secondary.label;
            }
            var selection = root.querySelector("[data-chart-selection]");
            if (selection) {
                selection.textContent = selected.length === 1 ? selected[0].label :
                    selected.length + " metrics / " + axes.length + (axes.length === 1 ? " axis" : " axes");
            }
            inputs.forEach(function (input) {
                var selectedAxes = axes.map(function (axis) { return axis.key; });
                var candidateAxis = axisFor(byKey[input.dataset.metric]).key;
                input.disabled = !input.checked && selectedAxes.length >= 2 &&
                    selectedAxes.indexOf(candidateAxis) === -1;
                input.closest("label").classList.toggle("is-unavailable", input.disabled);
            });
            chart.update();
        }

        inputs.forEach(function (input) {
            input.addEventListener("change", function () {
                if (input.checked) {
                    var selectedAxes = order.map(function (item) {
                        return axisFor(byKey[item.dataset.metric]).key;
                    });
                    var candidateAxis = axisFor(byKey[input.dataset.metric]).key;
                    if (selectedAxes.indexOf(candidateAxis) === -1 && new Set(selectedAxes).size >= 2) {
                        input.checked = false;
                    } else order.push(input);
                } else {
                    order = order.filter(function (item) { return item !== input; });
                    if (!order.length) {
                        input.checked = true;
                        order = [input];
                    }
                }
                sync();
            });
        });

        sync();

        // Team-page charts live inside a tab that starts at display:none.
        // Chart.js cannot establish reliable pointer hitboxes at zero width,
        // so resize once the tab becomes visible (and whenever its container
        // changes size) before accepting hover/click interaction.
        function refreshGeometry() {
            if (!root.offsetParent) return;
            window.requestAnimationFrame(function () { chart.resize(); chart.update("none"); });
        }
        document.addEventListener("cfb:tabschanged", refreshGeometry);
        if (typeof ResizeObserver !== "undefined") {
            new ResizeObserver(refreshGeometry).observe(root);
        }
    });

    document.querySelectorAll("[data-scatter-chart]").forEach(function (root) {
        var canvas = root.querySelector("canvas");
        var scatter = JSON.parse(root.querySelector("[data-scatter-json]").textContent);
        var playerUrlPrefix = root.dataset.playerUrl || "/nfl/players/";
        var points = scatter.points;
        var chart = new Chart(canvas.getContext("2d"), {
            type: "scatter",
            data: {
                datasets: [{
                    data: points.map(function (point) { return { x: point.x, y: point.y }; }),
                    backgroundColor: points.map(function (point) { return point.color; }),
                    borderColor: points.map(function (point) { return point.ring_color; }),
                    borderWidth: 1.5,
                    pointRadius: 5,
                    pointHoverRadius: 7,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                onClick: function (event, elements) {
                    if (!elements.length) return;
                    var point = points[elements[0].index];
                    if (point && point.player_id) {
                        window.location.href = playerUrlPrefix + point.player_id + "/";
                    }
                },
                onHover: function (event, elements) {
                    event.native.target.style.cursor = elements.length ? "pointer" : "default";
                },
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            title: function (items) {
                                if (!items.length) return "";
                                var point = points[items[0].dataIndex];
                                return point ? point.player_name + " · " + point.team +
                                    (point.position ? " " + point.position : "") : "";
                            },
                            label: function (item) {
                                var point = points[item.dataIndex];
                                if (!point) return "";
                                return [
                                    scatter.x_label + ": " + format(point.x, scatter.x_format),
                                    scatter.y_label + ": " + format(point.y, scatter.y_format),
                                ];
                            },
                        },
                    },
                },
                scales: {
                    x: {
                        title: { display: true, text: scatter.x_label },
                        ticks: { callback: function (v) { return format(v, scatter.x_format); } },
                    },
                    y: {
                        title: { display: true, text: scatter.y_label },
                        ticks: { callback: function (v) { return format(v, scatter.y_format); } },
                    },
                },
            },
        });
    });
}());
