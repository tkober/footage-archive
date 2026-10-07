"""Tests for the Windows opener (#124, #97):
footage-archive-opener-windows.ps1 embeds its real logic ("opener.ps1") and
a VBScript console-hiding wrapper ("launch.vbs") as single-quoted
here-strings, so that it can write them out verbatim on a PC. This file
extracts the opener.ps1 heredoc and runs it directly here on Linux with
PowerShell 7 (pwsh), a fake $env:APPDATA, and a fake Photoshop "exe" (a
bash script) configured via apps.photoshop.exe — so Resolve-AppExe never
has to touch the Windows registry. None of this touches a real PC; the
registry/wscript/Photoshop-detection checks happen on the actual hardware
after merge (see the issue).
"""

import json
import os
import shutil
import stat
import subprocess
import urllib.parse
from pathlib import Path

import pytest

SETUP_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / 'frontend' / 'public' / 'opener' / 'footage-archive-opener-windows.ps1'
)

_HEREDOC_START = "$OpenerSource = @'"
_HEREDOC_END = "'@"


def _find_pwsh():
    found = shutil.which('pwsh')
    if found:
        return found
    candidate = Path.home() / 'tools' / 'pwsh' / 'pwsh'
    if candidate.exists():
        return str(candidate)
    return None


PWSH = _find_pwsh()

if PWSH is None:
    pytest.skip("pwsh not available", allow_module_level=True)


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
    assert start is not None and end is not None, 'could not find the opener.ps1 heredoc in the setup script'

    opener_path = tmp_path / 'opener.ps1'
    opener_path.write_text(''.join(lines[start:end]), encoding='utf-8')
    return opener_path


@pytest.fixture
def opener_script(tmp_path):
    return _extract_opener_script(tmp_path)


def _write_config(appdata_dir: Path, root: Path, photoshop_exe: Path = None) -> None:
    config_dir = appdata_dir / 'FootageArchiveOpener'
    config_dir.mkdir(parents=True, exist_ok=True)
    apps = {'photoshop': {}}
    if photoshop_exe is not None:
        apps['photoshop']['exe'] = str(photoshop_exe)
    config = {'root': str(root), 'apps': apps}
    (config_dir / 'config.json').write_text(json.dumps(config, indent=2), encoding='utf-8')


@pytest.fixture
def fake_photoshop(tmp_path):
    """A fake Photoshop executable: a bash script that appends each of its
    arguments (one per line) to a log file. Windows PowerShell 5.1 needs
    Start-Process to pass the path wrapped in literal double quotes (for
    paths containing spaces); on this Linux pwsh build, Start-Process does
    not hand those quote characters through to the child process, but the
    fake strips them anyway in case that changes, rather than the opener
    script's quoting being adjusted for a test-only environment."""
    log = tmp_path / 'photoshop.log'
    exe = tmp_path / 'fake_photoshop.sh'
    script = (
        '#!/usr/bin/env bash\n'
        'for a in "$@"; do\n'
        '  v="$a"\n'
        '  v="${v#\\"}"\n'
        '  v="${v%\\"}"\n'
        '  printf \'%s\\n\' "$v" >> "' + str(log) + '"\n'
        'done\n'
    )
    exe.write_text(script, encoding='utf-8')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe, log


@pytest.fixture
def fake_home(tmp_path, fake_photoshop):
    """A fake $env:APPDATA whose config.json points `root` at tmp_path/share,
    which holds a Unicode + space path on purpose (japan_2024/photo/熱海
    atami/P1000123.RW2) and a notes.txt (an unsupported extension), plus
    apps.photoshop.exe pointing at the fake Photoshop script."""
    appdata = tmp_path / 'appdata'
    share = tmp_path / 'share'
    exe, _log = fake_photoshop

    photo_dir = share / 'japan_2024' / 'photo' / '熱海 atami'
    photo_dir.mkdir(parents=True)
    (photo_dir / 'P1000123.RW2').write_bytes(b'fake-raw-bytes')
    (share / 'notes.txt').write_text('not an image', encoding='utf-8')

    _write_config(appdata, share, photoshop_exe=exe)
    return appdata, share


def _run(opener_script, appdata, url):
    env = dict(os.environ)
    env['APPDATA'] = str(appdata)
    # pwsh needs HOME to initialize; keep whatever the ambient one is.
    env.setdefault('HOME', os.environ.get('HOME', '/tmp'))
    return subprocess.run(
        [PWSH, '-NoProfile', '-File', str(opener_script), '-Url', url],
        env=env, capture_output=True, text=True,
    )


def _encode_path(*segments):
    return '/'.join(urllib.parse.quote(s, safe='') for s in segments)


def test_open_rw2_with_unicode_and_space(opener_script, fake_home, fake_photoshop):
    appdata, share = fake_home
    _exe, log = fake_photoshop
    path = _encode_path('japan_2024', 'photo', '熱海 atami', 'P1000123.RW2')
    result = _run(opener_script, appdata, f'footage-archive://open?app=photoshop&path={path}')

    assert result.returncode == 0, result.stderr
    log_text = log.read_text(encoding='utf-8')
    expected_full_path = str(share / 'japan_2024' / 'photo' / '熱海 atami' / 'P1000123.RW2')
    assert expected_full_path in log_text


def test_open_rejects_dotdot_segment(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(
        opener_script, appdata,
        'footage-archive://open?app=photoshop&path=japan_2024/../etc/passwd',
    )

    assert result.returncode == 1
    assert 'Invalid path' in result.stderr


def test_open_rejects_absolute_path(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(
        opener_script, appdata,
        'footage-archive://open?app=photoshop&path=%2Fetc%2Fpasswd',
    )

    assert result.returncode == 1
    assert 'Invalid path' in result.stderr


def test_open_rejects_drive_letter_path(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(
        opener_script, appdata,
        'footage-archive://open?app=photoshop&path=' + urllib.parse.quote('C:/etc/passwd', safe=''),
    )

    assert result.returncode == 1
    assert 'Invalid path' in result.stderr


def test_open_rejects_unsupported_extension(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(opener_script, appdata, 'footage-archive://open?app=photoshop&path=notes.txt')

    assert result.returncode == 1
    assert 'not supported' in result.stderr


def test_open_missing_file(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(
        opener_script, appdata,
        'footage-archive://open?app=photoshop&path=japan_2024/photo/missing.rw2',
    )

    assert result.returncode == 1
    assert 'File not found' in result.stderr


def test_open_unknown_app(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(opener_script, appdata, 'footage-archive://open?app=lightroom&path=notes.txt')

    assert result.returncode == 1
    assert 'Unknown app' in result.stderr


def test_action_test_reports_root_and_app(opener_script, fake_home):
    appdata, _share = fake_home
    result = _run(opener_script, appdata, 'footage-archive://test')

    assert result.returncode == 0, result.stderr
    assert 'Root:' in result.stdout
    assert 'OK' in result.stdout


def test_root_not_reachable(opener_script, tmp_path):
    appdata = tmp_path / 'appdata_no_share'
    missing_root = tmp_path / 'share_not_connected'
    _write_config(appdata, missing_root)

    test_result = _run(opener_script, appdata, 'footage-archive://test')
    assert test_result.returncode == 0, test_result.stderr
    assert 'not reachable' in test_result.stdout

    open_result = _run(opener_script, appdata, 'footage-archive://open?app=photoshop&path=foo.jpg')
    assert open_result.returncode == 1
    assert 'Share not reachable' in open_result.stderr


def _parse_syntax_errors(path: Path):
    script = (
        "$errors = $null; $tokens = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$tokens, [ref]$errors); "
        "$errors.Count"
    )
    result = subprocess.run([PWSH, '-NoProfile', '-Command', script], capture_output=True, text=True)
    return result


def test_setup_script_passes_powershell_syntax_check():
    result = _parse_syntax_errors(SETUP_SCRIPT)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0', result.stdout


def test_extracted_opener_passes_powershell_syntax_check(opener_script):
    result = _parse_syntax_errors(opener_script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '0', result.stdout
