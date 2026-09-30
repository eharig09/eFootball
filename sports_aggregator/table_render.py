"""Fast server-side rendering of a statistical table's body rows.

The `data_table` macro in templates/_tables.html costs about 0.4 ms per cell in Jinja (eleven row lookups
and string concatenations per cell, almost all of them for decorations that most cells do not have), which
made a 300,000-cell page take two minutes. This renders the same `<tr>`/`<td>` markup directly.

It must stay byte-equivalent (modulo insignificant whitespace) to the macro's loop; tests/test_table_render.py
renders both and compares. The one decoration it does not handle is `<key>_conference`, which needs the
macro's `conference_mark` and `url_for`: `render_rows` returns None then and the macro falls back to its own
loop for that table.
"""

from __future__ import annotations

from typing import Any

from markupsafe import Markup, escape

from sports_aggregator.tables import Table, format_value

#: Mirrors STYLED_COLUMN_KEYS in templates/_tables.html (a test keeps the two in step).
STYLED_COLUMN_KEYS = frozenset({
    "away_offense", "away", "away_defense", "home_offense", "home", "home_defense", "edge", "advantage",
})

_DECORATION_SUFFIXES = ("_url", "_sub", "_class", "_scale_position", "_logo", "_logo_dark", "_color", "_color_dark")


def _external(href: Any) -> str:
    text = str(href)
    return ' target="_blank" rel="noopener"' if text.startswith(("http://", "https://")) else ""


def render_rows(table: Table) -> Markup | None:
    """The `<tr>` elements for every row of `table`, or None when the macro must render them."""
    columns = list(table.columns)
    rows = table.rows
    conference_keys = tuple(f"{column.key}_conference" for column in columns)
    for row in rows:
        for key in conference_keys:
            if row.get(key):
                return None

    plan = []
    for column in columns:
        key = column.key
        base = f"col-{column.align}"
        if key in STYLED_COLUMN_KEYS:
            base += f" col-key-{key.replace('_', '-')}"
        if column.numeric:
            base += " num"
        if column.emphasis:
            base += " emphasis"
        plan.append((key, column.format, str(escape(base)), key + "_sort",
                     tuple(key + suffix for suffix in _DECORATION_SUFFIXES)))

    out: list[str] = []
    append = out.append
    for row in rows:
        get = row.get
        row_class = get("_row_class")
        append(f'<tr class="{escape(row_class)}">' if row_class else "<tr>")
        for key, fmt, base_class, sort_key, decoration_keys in plan:
            value = get(key)
            text = format_value(value, fmt)
            sort_value = get(sort_key, value) if value is not None else ""
            if value is None:
                sort_value = ""
            url_key, sub_key, class_key, scale_key, logo_key, logo_dark_key, color_key, color_dark_key = decoration_keys
            href, sub, state_class, scale_position = get(url_key), get(sub_key), get(class_key), get(scale_key)
            logo, color = get(logo_key), get(color_key)

            classes = base_class
            if text == "—":
                classes += " is-empty"
            if state_class:
                classes += f" {escape(state_class)}"
            attributes = ' title="Comparison edge"' if state_class == "advantage" else ""
            if sort_value is not None and str(sort_value) and str(sort_value) != text:
                attributes += f' data-sort-value="{escape(sort_value)}"'
            append(f'<td class="{classes}"{attributes}>')

            escaped = escape(text)
            if logo or color:
                append('<span class="team-cell"')
                if color:
                    color_dark = get(color_dark_key)
                    append(f' data-identity style="--row-team-light:{escape(color)};'
                           f'--row-team-dark:{escape(color_dark or color)}"')
                append(">")
                if color:
                    append('<i class="team-rule" aria-hidden="true"></i>')
                if logo:
                    logo_dark = get(logo_dark_key)
                    append('<span class="team-logo team-mark" aria-hidden="true" '
                           f"style=\"--mark:url('{escape(logo)}');--mark-dark:url('{escape(logo_dark or logo)}')\"></span>")
                if href:
                    append(f'<a href="{escape(href)}"{_external(href)}>{escaped}</a>')
                else:
                    append(f"<span>{escaped}</span>")
                append("</span>")
            elif href:
                append(f'<a href="{escape(href)}"{_external(href)}>{escaped}</a>')
            else:
                append(str(escaped))
            if sub:
                append(f'<span class="sub">{escape(sub)}</span>')
            if scale_position is not None:
                side = get(key + "_scale_side", "neutral")
                left = get(key + "_scale_left", 50)
                width = get(key + "_scale_width", 0)
                label = get(key + "_scale_label", "Centered comparison scale")
                edge_color = f"--edge-color:{escape(color)};" if color else ""
                # The macro leaves the newline after `{%- if scale_position ... %}` in place, which
                # collapses to one space between the cell text and the scale.
                append(f' <span class="comparison-scale scale-{escape(side)}" '
                       f'style="--edge-position:{escape(scale_position)}%;--edge-left:{escape(left)}%;'
                       f'--edge-width:{escape(width)}%;{edge_color}" role="img" aria-label="{escape(label)}">'
                       '<i class="comparison-scale-fill" aria-hidden="true"></i>'
                       '<i class="comparison-scale-marker" aria-hidden="true"></i></span>')
            append("</td>")
        append("</tr>")
    return Markup("".join(out))
