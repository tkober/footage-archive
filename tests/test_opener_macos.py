"""Tests for the macOS opener (#123, #97): footage-archive-opener-macos.sh
embeds its real logic ("opener.sh") as a quoted heredoc so that it can be
written out verbatim on a Mac. This file extracts that heredoc and runs it
directly here on Linux, with a fake HOME, a fake `open`/`osascript` on PATH,
and no `plutil` on PATH — so `cfg_get` in opener.sh falls back to its
python3 JSON walker, exactly as it would on this CI box. None of this touches
a real Mac; the manual Gatekeeper/Chrome-policy/Photoshop check happens on
the actual hardware after merge (see the issue)."""

import os
import stat
import subprocess
import urllib.parse
from pathlib import Path

import pytest

SETUP_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / 'frontend' / 'public' / 'opener' / 'footage-archive-opener-macos.sh'
)

_HEREDOC_START = "<<'OPENER_EOF'"
_HEREDOC_END = 'OPENER_EOF'


def _extract_opener_script(tmp_path: Path) -> Path:
    lines = SETUP_SCRIPT.read_text(encoding='utf-8').splitlines(keepends=True)
    start = None
    end = None
    for i, line in enumerate(lines):
        if start is None and _HEREDOC_START in line:
            start = i + 1
        elif start is not None and line.rstrip('\n') == _HEREDOC_END:
            end = i
            break
    assert start is not None and end is not None, 'could not find the opener.sh heredoc in the setup script'

    opener_path = tmp_path / 'opener.sh'
    opener_path.write_text(''.join(lines[start:end]), encoding='utf-8')
    opener_path.chmod(opener_path.stat().st_mode | stat.S_IEXEC)
    return opener_path


@pytest.fixture
def opener_script(tmp_path):
    return _extract_opener_script(tmp_path)


def _write_config(support_dir: Path, root: Path) -> None:
    support_dir.mkdir(parents=True, exist_ok=True)
    (support_dir / 'config.json').write_text(
        '{\n  "root": "%s",\n  "apps": {\n    "photoshop": {}\n  }\n}\n' % str(root),
        encoding='utf-8',
    )


@pytest.fixture
def fake_home(tmp_path):
    """A fake $HOME whose config.json points `root` at tmp_path/share, which
    holds a Unicode + space path on purpose (japan_2024/photo/熱海 atami/
    P1000123.RW2) and a notes.txt (an unsupported extension)."""
    home = tmp_path / 'home'
    share = tmp_path / 'share'

    photo_dir = share / 'japan_2024' / 'photo' / '熱海 atami'
    photo_dir.mkdir(parents=True)
    (photo_dir / 'P1000123.RW2').write_bytes(b'fake-raw-bytes')
    (share / 'notes.txt').write_text('not an image', encoding='utf-8')

    _write_config(home / 'Library' / 'Application Support' / 'FootageArchiveOpener', share)
    return home


@pytest.fixture
def fake_bin(tmp_path):
    """A PATH directory holding a fake `open` (appends its args to a log
    file) and a fake `osascript` (reports Photoshop as installed) — and no
    `plutil`, which forces opener.sh's python3 config-reading fallback."""
    bindir = tmp_path / 'fakebin'
    bindir.mkdir()
    log = tmp_path / 'open.log'

    open_bin = bindir / 'open'
    open_bin.write_text(f'#!/usr/bin/env bash\necho "$*" >> "{log}"\n', encoding='utf-8')
    open_bin.chmod(open_bin.stat().st_mode | stat.S_IEXEC)

    osascript_bin = bindir / 'osascript'
    osascript_bin.write_text(
        '#!/usr/bin/env bash\necho "/Applications/Adobe Photoshop 2025/Adobe Photoshop 2025.app"\n',
        encoding='utf-8',
    )
    osascript_bin.chmod(osascript_bin.stat().st_mode | stat.S_IEXEC)

    return bindir, log


def _run(opener_script, home, bindir, url):
    env = dict(os.environ)
    env['HOME'] = str(home)
    env['PATH'] = f'{bindir}:/usr/bin:/bin'
    return subprocess.run([str(opener_script), url], env=env, capture_output=True, text=True)


def _encode_path(*segments):
    return '/'.join(urllib.parse.quote(s, safe='') for s in segments)


def test_open_rw2_with_unicode_and_space(opener_script, fake_home, fake_bin):
    bindir, log = fake_bin
    path = _encode_path('japan_2024', 'photo', '熱海 atami', 'P1000123.RW2')
    result = _run(opener_script, fake_home, bindir, f'footage-archive://open?app=photoshop&path={path}')

    assert result.returncode == 0, result.stderr
    log_text = log.read_text(encoding='utf-8')
    assert '-b com.adobe.Photoshop' in log_text
    assert '熱海 atami/P1000123.RW2' in log_text


def test_open_rejects_dotdot_segment(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(
        opener_script, fake_home, bindir,
        'footage-archive://open?app=photoshop&path=japan_2024/../etc/passwd',
    )

    assert result.returncode == 1
    assert 'Invalid path' in result.stderr


def test_open_rejects_absolute_path(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(
        opener_script, fake_home, bindir,
        'footage-archive://open?app=photoshop&path=%2Fetc%2Fpasswd',
    )

    assert result.returncode == 1
    assert 'Invalid path' in result.stderr


def test_open_rejects_unsupported_extension(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(opener_script, fake_home, bindir, 'footage-archive://open?app=photoshop&path=notes.txt')

    assert result.returncode == 1
    assert 'not supported' in result.stderr


def test_open_missing_file(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(
        opener_script, fake_home, bindir,
        'footage-archive://open?app=photoshop&path=japan_2024/photo/missing.rw2',
    )

    assert result.returncode == 1
    assert 'File not found' in result.stderr


def test_open_unknown_app(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(opener_script, fake_home, bindir, 'footage-archive://open?app=lightroom&path=notes.txt')

    assert result.returncode == 1


def test_action_test_reports_root_and_app_with_checkmarks(opener_script, fake_home, fake_bin):
    bindir, _log = fake_bin
    result = _run(opener_script, fake_home, bindir, 'footage-archive://test')

    assert result.returncode == 0, result.stderr
    assert 'Root:' in result.stdout
    assert '✓' in result.stdout


def test_root_not_mounted(opener_script, tmp_path, fake_bin):
    bindir, _log = fake_bin
    home = tmp_path / 'home_no_share'
    missing_root = tmp_path / 'share_not_mounted'
    _write_config(home / 'Library' / 'Application Support' / 'FootageArchiveOpener', missing_root)

    test_result = _run(opener_script, home, bindir, 'footage-archive://test')
    assert test_result.returncode == 0, test_result.stderr
    assert '✗' in test_result.stdout

    open_result = _run(opener_script, home, bindir, 'footage-archive://open?app=photoshop&path=foo.jpg')
    assert open_result.returncode == 1
    assert 'Share not mounted' in open_result.stderr


def test_setup_script_passes_bash_syntax_check():
    result = subprocess.run(['bash', '-n', str(SETUP_SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_extracted_opener_passes_bash_syntax_check(opener_script):
    result = subprocess.run(['bash', '-n', str(opener_script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_setup_script_help_prints_usage_and_exits_cleanly(tmp_path):
    fake_home = tmp_path / 'home'
    fake_home.mkdir()
    env = dict(os.environ)
    env['HOME'] = str(fake_home)

    result = subprocess.run(['bash', str(SETUP_SCRIPT), '--help'], capture_output=True, text=True, env=env)

    assert result.returncode == 0, result.stderr
    assert 'Usage' in result.stdout
    assert list(fake_home.iterdir()) == []
