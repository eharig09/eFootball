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

    function buildDataset(chartData, axisId) {
        return {
            label: chartData.label,
            data: chartData.values.map(function (point) { return point.value; }),
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
                    var point = chartData && chartData.values[el.index];
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
                                var input = order[items[0].datasetIndex];
                                var chartData = input && byKey[input.dataset.metric];
                                var point = chartData && chartData.values[items[0].dataIndex];
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
            var primary = byKey[order[0].dataset.metric];
            var secondary = order[1] ? byKey[order[1].dataset.metric] : null;
            chart.data.labels = primary.values.map(function (point) { return point.week_label; });
            chart.data.datasets = order.map(function (input, index) {
                return buildDataset(byKey[input.dataset.metric], index === 0 ? "y" : "y1");
            });
            var yScale = chart.options.scales.y;
            yScale.ticks.callback = function (v) { return format(v, primary.format); };
            yScale.ticks.color = primary.color;
            yScale.title.text = primary.label;
            yScale.title.color = primary.color;
            var y1Scale = chart.options.scales.y1;
            y1Scale.display = !!secondary;
            if (secondary) {
                y1Scale.ticks.callback = function (v) { return format(v, secondary.format); };
                y1Scale.ticks.color = secondary.color;
                y1Scale.title.text = secondary.label;
                y1Scale.title.color = secondary.color;
            }
            chart.update();
        }

        inputs.forEach(function (input) {
            input.addEventListener("change", function () {
                if (input.checked) {
                    order.push(input);
                    if (order.length > 2) {
                        var dropped = order.shift();
                        dropped.checked = false;
                    }
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
