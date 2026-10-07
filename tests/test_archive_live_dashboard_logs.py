import zipfile

from scripts.archive_live_dashboard_logs import archive_session


def test_archive_session_verifies_zip_before_removing_source(tmp_path):
    root = tmp_path / 'logs' / 'live_dashboard'
    source = root / '20200101_010203_123456'
    archive_dir = root / 'archive'
    source.mkdir(parents=True)
    archive_dir.mkdir()
    source.joinpath('session.jsonl').write_text('{"ok": true}\n', encoding='utf-8')
    source.joinpath('run_report.json').write_text('{"status": "done"}\n', encoding='utf-8')

    row = archive_session(source, archive_dir)

    output = archive_dir / '20200101_010203_123456.zip'
    assert row['session'] == source.name
    assert output.is_file()
    assert not source.exists()
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == ['run_report.json', 'session.jsonl']
