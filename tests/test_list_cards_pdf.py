from io import BytesIO

import pypdf

from exports.list_cards_pdf import render_list_cards_pdf, wrap_or_truncate_path


def _make_items(n: int) -> list[dict]:
    return [
        {
            'item_code': f'CODE{i:02d}'[:6],
            'directory': '/root/japan_2024/phone/kokura/photo',
            'file_name': f'2024103{i % 10}_1238{i:02d}.jpg',
        }
        for i in range(n)
    ]


def test_render_produces_pdf_bytes():
    pdf = render_list_cards_pdf('My List', _make_items(5), '/root')
    assert pdf.startswith(b'%PDF')


def test_render_two_pages_for_more_than_28_items():
    pdf = render_list_cards_pdf('My List', _make_items(30), '/root', cols=4, rows=7)
    reader = pypdf.PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 2


def test_render_single_page_for_28_or_fewer_items():
    pdf = render_list_cards_pdf('My List', _make_items(28), '/root', cols=4, rows=7)
    reader = pypdf.PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 1


def test_render_empty_list_yields_single_page():
    pdf = render_list_cards_pdf('Empty List', [], '/root')
    reader = pypdf.PdfReader(BytesIO(pdf))
    assert len(reader.pages) == 1


def test_wrap_short_path_fits_one_line():
    lines = wrap_or_truncate_path('a/b.jpg', max_width=200, font_size=6)
    assert lines == ['a/b.jpg']


def test_wrap_medium_path_wraps_to_two_lines():
    path = 'japan_2024/phone/kokura/photo/20241030_123817.jpg'
    lines = wrap_or_truncate_path(path, max_width=90, font_size=6)
    assert 1 <= len(lines) <= 2
    assert ''.join(lines).replace('…', '') in path or lines[-1].endswith('.jpg')


def test_wrap_long_path_truncates_from_start_and_keeps_file_name():
    path = 'japan_2024/' + '/'.join(f'very_long_directory_segment_{i}' for i in range(20)) + '/final_photo_0001.jpg'
    lines = wrap_or_truncate_path(path, max_width=90, font_size=6)
    joined = ''.join(lines)
    assert joined.startswith('…')
    assert joined.endswith('final_photo_0001.jpg')

    from reportlab.pdfbase.pdfmetrics import stringWidth
    for line in lines:
        assert stringWidth(line, 'Helvetica', 6) <= 90 + 1e-6
    assert len(lines) <= 2
