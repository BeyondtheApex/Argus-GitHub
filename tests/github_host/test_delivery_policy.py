from argus.github_host.delivery import requires_manual_review


def test_self_modification_and_incomplete_file_inventories_cannot_automerge():
    assert requires_manual_review({'changed_files': 2}, [{'filename': 'research/report.md'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': 'argus/__init__.py'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': '.github/actions/setup/action.yml'}])
    assert requires_manual_review({'changed_files': 1}, [{'filename': 'docs/old.py', 'previous_filename': 'argus/core/session.py'}])
    assert not requires_manual_review({'changed_files': 1}, [{'filename': 'host-fixtures/example.txt'}])
