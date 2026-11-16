from __future__ import annotations

import json
import time

from ozagia.daemon.server import DaemonState


def test_hint_rejects_unbounded_input_and_expired_request(tmp_path):
    state = DaemonState(tmp_path, object(), tmp_path / 'journal.jsonl')
    assert not state.dispatch({'op': 'hint', 'prompt': 'x' * 12001})['ok']
    assert not state.dispatch({'op': 'hint', 'prompt': 'x', 'history': [
        {'role': 'tool', 'text': 'not a conversation hint'}]})['ok']
    result = state.dispatch({'op': 'hint', 'prompt': 'x',
                             'deadline_ms': (time.time() - 1) * 1000})
    assert result['expired'] and result['context'] == ''


def test_hint_is_bounded_to_daemon_and_journal_does_not_store_text(tmp_path, monkeypatch):
    from ozagia.core import proactive_context
    state = DaemonState(tmp_path, object(), tmp_path / 'journal.jsonl')
    monkeypatch.setattr(proactive_context, 'build_context', lambda memory, prompt, **kwargs: {
        'context': 'candidate context', 'keys': ['file:a.py']})
    message = {'op': 'hint', 'prompt': 'secret task words',
               'history': [{'role': 'user', 'text': 'private previous message'}],
               'session': 'session-1'}
    result = state.dispatch(message)
    assert result['ok'] and result['context'] == 'candidate context'
    state.record(message, result, 1.0)
    raw = (tmp_path / 'journal.jsonl').read_text()
    assert 'secret task words' not in raw and 'private previous message' not in raw
    assert json.loads(raw)['keys'] == ['file:a.py']
