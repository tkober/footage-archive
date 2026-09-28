"""PDF export of a list's items as cut-out cards for gachapon capsules.

Pure module: no DB access, no FastAPI imports. `render_list_cards_pdf` takes
plain data (list name, item dicts, root dir) and returns PDF bytes.

Layout notes (tuned by eye against a rendered preview, see tests/CLAUDE.md):
- A4 portrait, ~10 mm margins on all sides; the card grid fills the full
  usable area (margin to margin) and the footer sits *inside* the bottom
  margin band, so it doesn't steal space from the grid.
- Grid lines are thin (0.3 pt) and very light (#DDDDDD) — cutting guides,
  not a table. They're drawn as shared lines between cells (never as
  per-cell rectangles) so there are no doubled-up lines at cell borders.
- Each card: the item code dominates (bold, large, letter-spaced for
  legibility when handwritten/typed back in later), the relative path is a
  small grey caption below it, wrapped to at most two lines or, if it still
  doesn't fit, truncated from the *start* (the file name at the end matters
  most for identifying the shot).
"""
from __future__ import annotations

import math
from datetime import date
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

CODE_FONT = 'Helvetica-Bold'
PATH_FONT = 'Helvetica'
FOOTER_FONT = 'Helvetica'

GRID_LINE_WIDTH = 0.3
GRID_LINE_COLOR = colors.HexColor('#DDDDDD')
PATH_COLOR = colors.HexColor('#777777')
FOOTER_COLOR = colors.HexColor('#AAAAAA')
CODE_COLOR = colors.black

MARGIN = 10 * mm
CODE_FONT_SIZE_MAX = 28
CODE_FONT_SIZE_MIN = 12
CODE_CHAR_SPACE_MAX = 2.2
PATH_FONT_SIZE = 6
FOOTER_FONT_SIZE = 6
CELL_H_PADDING = 5  # horizontal padding inside a cell, in points
PATH_LINE_HEIGHT = 7.0
PATH_MAX_LINES = 2

ELLIPSIS = '…'


def _relative_path(item: dict, root_dir: str) -> str:
    directory = item.get('directory') or ''
    file_name = item.get('file_name') or ''
    root = (root_dir or '').rstrip('/')
    rel_dir = directory
    if root and rel_dir.startswith(root):
        rel_dir = rel_dir[len(root):].lstrip('/')
    return f'{rel_dir}/{file_name}' if rel_dir else file_name


def _take_line(text: str, max_width: float, font_name: str, font_size: float) -> tuple[str, str]:
    """Split off the longest prefix of `text` that fits `max_width`, preferring
    to break right after a '/' near the fit point for a nicer wrap."""
    lo, hi = 0, len(text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        if stringWidth(text[:mid], font_name, font_size) <= max_width:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if best == 0:
        best = 1  # always make progress, even if a single char overflows
    if best >= len(text):
        return text, ''
    slash_idx = text.rfind('/', 0, best)
    cut = slash_idx + 1 if slash_idx != -1 and slash_idx > best * 0.4 else best
    if cut == 0:
        cut = best
    return text[:cut], text[cut:]


def _char_wrap(text: str, max_width: float, font_name: str, font_size: float,
               max_lines: int) -> list[str] | None:
    """Greedily wrap `text` into at most `max_lines` lines each fitting
    `max_width`. Returns None if it doesn't all fit within `max_lines`."""
    lines: list[str] = []
    remaining = text
    for _ in range(max_lines):
        if not remaining:
            break
        line, remaining = _take_line(remaining, max_width, font_name, font_size)
        lines.append(line)
    if remaining:
        return None
    return lines or ['']


def wrap_or_truncate_path(path: str, max_width: float, font_name: str = PATH_FONT,
                           font_size: float = PATH_FONT_SIZE,
                           max_lines: int = PATH_MAX_LINES) -> list[str]:
    """Wrap `path` into at most `max_lines` lines fitting `max_width`. If it
    doesn't fit even wrapped, truncate from the START (dropping leading
    characters) and prefix with an ellipsis, since the file name at the end
    is the most important part to keep legible."""
    wrapped = _char_wrap(path, max_width, font_name, font_size, max_lines)
    if wrapped is not None:
        return wrapped

    lo, hi = 0, len(path) - 1
    best_drop = hi
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = ELLIPSIS + path[mid:]
        if _char_wrap(candidate, max_width, font_name, font_size, max_lines) is not None:
            best_drop = mid
            hi = mid - 1
        else:
            lo = mid + 1

    candidate = ELLIPSIS + path[best_drop:]
    result = _char_wrap(candidate, max_width, font_name, font_size, max_lines)
    return result if result is not None else [ELLIPSIS]


def _fit_code_style(code: str, available_width: float) -> tuple[float, float]:
    """Pick a (font_size, char_space) pair so `code` fits `available_width`,
    shrinking the font first and, only if still too wide, dropping the
    letter spacing too."""
    for font_size in _frange(CODE_FONT_SIZE_MAX, CODE_FONT_SIZE_MIN, -1):
        char_space = CODE_CHAR_SPACE_MAX * (font_size / CODE_FONT_SIZE_MAX)
        width = stringWidth(code, CODE_FONT, font_size) + char_space * max(len(code) - 1, 0)
        if width <= available_width:
            return font_size, char_space
    # Last resort: smallest font, no extra letter spacing.
    return CODE_FONT_SIZE_MIN, 0.0


def _frange(start: float, stop: float, step: float):
    value = start
    while value >= stop:
        yield value
        value += step


def _draw_centered_spaced_text(c: canvas.Canvas, text: str, center_x: float, y: float,
                                font_name: str, font_size: float, char_space: float,
                                color) -> None:
    # Character spacing (Tc) is PDF text *state*, not scoped to this text
    # object — it persists across BT/ET blocks until explicitly changed
    # again. So every text draw in this module (including this one) sets
    # its own charSpace explicitly, rather than relying on the canvas's
    # default (which would otherwise leak the code's letter-spacing into
    # whatever text is drawn next, e.g. the path caption below it).
    width = stringWidth(text, font_name, font_size) + char_space * max(len(text) - 1, 0)
    start_x = center_x - width / 2
    text_obj = c.beginText(start_x, y)
    text_obj.setFont(font_name, font_size)
    text_obj.setCharSpace(char_space)
    text_obj.setFillColor(color)
    text_obj.textOut(text)
    c.drawText(text_obj)


def _draw_centered_text(c: canvas.Canvas, text: str, center_x: float, y: float,
                         font_name: str, font_size: float, color) -> None:
    """Like canvas.drawCentredString, but resets charSpace to 0 explicitly
    (see _draw_centered_spaced_text) so it never inherits leftover state."""
    _draw_centered_spaced_text(c, text, center_x, y, font_name, font_size, 0.0, color)


def _draw_card(c: canvas.Canvas, x: float, y: float, w: float, h: float,
               item: dict, root_dir: str) -> None:
    code = (item.get('item_code') or '').upper()
    available_code_width = w - 2 * CELL_H_PADDING
    font_size, char_space = _fit_code_style(code, available_code_width)
    code_y = y + h * 0.58
    _draw_centered_spaced_text(c, code, x + w / 2, code_y, CODE_FONT, font_size, char_space, CODE_COLOR)

    rel_path = _relative_path(item, root_dir)
    max_text_width = w - 2 * CELL_H_PADDING
    lines = wrap_or_truncate_path(rel_path, max_text_width)
    path_top_y = y + h * 0.34
    for i, line in enumerate(lines):
        ly = path_top_y - i * PATH_LINE_HEIGHT
        _draw_centered_text(c, line, x + w / 2, ly, PATH_FONT, PATH_FONT_SIZE, PATH_COLOR)


def _draw_grid_lines(c: canvas.Canvas, x0: float, top: float, cell_w: float, cell_h: float,
                      cols: int, n_items: int) -> None:
    n_items = max(n_items, 1)
    used_rows = math.ceil(n_items / cols)
    last_row_items = n_items - (used_rows - 1) * cols
    has_partial = last_row_items != cols
    full_block_rows = used_rows - 1 if has_partial else used_rows

    c.saveState()
    c.setLineWidth(GRID_LINE_WIDTH)
    c.setStrokeColor(GRID_LINE_COLOR)

    if full_block_rows > 0:
        block_bottom = top - full_block_rows * cell_h
        for col in range(cols + 1):
            x = x0 + col * cell_w
            c.line(x, top, x, block_bottom)
        for r in range(full_block_rows + 1):
            y = top - r * cell_h
            c.line(x0, y, x0 + cols * cell_w, y)

    if has_partial:
        row_top = top - full_block_rows * cell_h
        row_bottom = row_top - cell_h
        width_used = last_row_items * cell_w
        for col in range(last_row_items + 1):
            x = x0 + col * cell_w
            c.line(x, row_top, x, row_bottom)
        c.line(x0, row_bottom, x0 + width_used, row_bottom)
        if full_block_rows == 0:
            c.line(x0, row_top, x0 + width_used, row_top)

    c.restoreState()


def _draw_footer(c: canvas.Canvas, page_w: float, margin: float, list_name: str,
                  today: str, page_no: int, page_count: int) -> None:
    baseline_y = margin * 0.4
    text_obj = c.beginText(margin, baseline_y)
    text_obj.setFont(FOOTER_FONT, FOOTER_FONT_SIZE)
    text_obj.setCharSpace(0)
    text_obj.setFillColor(FOOTER_COLOR)
    text_obj.textOut(list_name)
    c.drawText(text_obj)
    _draw_right_aligned_text(c, f'{today} · page {page_no}/{page_count}',
                              page_w - margin, baseline_y, FOOTER_FONT, FOOTER_FONT_SIZE, FOOTER_COLOR)


def _draw_right_aligned_text(c: canvas.Canvas, text: str, right_x: float, y: float,
                              font_name: str, font_size: float, color) -> None:
    width = stringWidth(text, font_name, font_size)
    text_obj = c.beginText(right_x - width, y)
    text_obj.setFont(font_name, font_size)
    text_obj.setCharSpace(0)
    text_obj.setFillColor(color)
    text_obj.textOut(text)
    c.drawText(text_obj)


def _draw_empty_note(c: canvas.Canvas, page_w: float, page_h: float) -> None:
    _draw_centered_text(c, 'This list is empty', page_w / 2, page_h / 2, PATH_FONT, 10, PATH_COLOR)


def render_list_cards_pdf(list_name: str, items: list[dict], root_dir: str,
                           cols: int = 4, rows: int = 7) -> bytes:
    """Render an A4 PDF of cut-out cards, one per item, `cols` x `rows` per
    page (defaults 4x7 = 28/page). Items are placed in the given order (the
    caller is expected to have sorted them, e.g. by item_code)."""
    buf = BytesIO()
    page_w, page_h = A4
    c = canvas.Canvas(buf, pagesize=A4)

    grid_left = MARGIN
    grid_top = page_h - MARGIN
    usable_w = page_w - 2 * MARGIN
    usable_h = page_h - 2 * MARGIN
    cell_w = usable_w / cols
    cell_h = usable_h / rows
    per_page = cols * rows

    today = date.today().isoformat()

    if not items:
        _draw_empty_note(c, page_w, page_h)
        _draw_footer(c, page_w, MARGIN, list_name, today, 1, 1)
        c.showPage()
        c.save()
        return buf.getvalue()

    page_count = max(1, math.ceil(len(items) / per_page))
    for page_idx in range(page_count):
        page_items = items[page_idx * per_page: (page_idx + 1) * per_page]
        _draw_grid_lines(c, grid_left, grid_top, cell_w, cell_h, cols, len(page_items))
        for i, item in enumerate(page_items):
            row, col = divmod(i, cols)
            cell_x = grid_left + col * cell_w
            cell_y = grid_top - (row + 1) * cell_h
            _draw_card(c, cell_x, cell_y, cell_w, cell_h, item, root_dir)
        _draw_footer(c, page_w, MARGIN, list_name, today, page_idx + 1, page_count)
        c.showPage()

    c.save()
    return buf.getvalue()
