import os

import pytest


@pytest.fixture(autouse=True)
def restore_native_environment():
    before = dict(os.environ)
    yield
    for key in before.keys() | os.environ.keys():
        if key.startswith(('ARGUS_', 'CODEX_', 'CLAUDE_')):
            if key in before:
                os.environ[key] = before[key]
            else:
                os.environ.pop(key, None)
