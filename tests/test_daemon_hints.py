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


class _RepoMapMemory:
    """The two DevMemory calls the repo-map hint makes, with call order."""

    def __init__(self, snapshot=None, map_result=None, error=None):
        self.snapshot = snapshot if snapshot is not None else {
            'freshness': 'fresh', 'stale': False}
        self.map_result = map_result if map_result is not None else {
            'ok': True,
            'markdown': '# Repo map\n\npkg/mod.py  score=1.0000\n  L1 add\n',
            'files': [{'path': 'pkg/mod.py', 'tier': 'full'},
                      {'path': 'tests/test_mod.py', 'tier': 'outline'}],
            'cache': {'root_hash': 'abc123', 'cached_files': 2,
                      'parsed': 1, 'reused': 1},
            'estimated_tokens': 20,
        }
        self.error = error
        self.calls = []

    def staleness(self, *, force=False):
        self.calls.append(('staleness', force))
        return dict(self.snapshot)

    def repo_map(self, task, *, budget_tokens=1500):
        self.calls.append(('repo_map', task, budget_tokens))
        if self.error:
            raise self.error
        return dict(self.map_result)


def _repo_map_state(tmp_path, memory):
    return DaemonState(tmp_path, memory, tmp_path / 'journal.jsonl')


def test_repo_map_hint_delivers_a_bounded_verified_map(tmp_path):
    memory = _RepoMapMemory()
    state = _repo_map_state(tmp_path, memory)
    result = state.dispatch({'op': 'hint', 'prompt': 'fix retry',
                             'repo_map_tokens': 500, 'session': 's1'})
    assert result['ok'] is True
    assert memory.calls[0] == ('staleness', True)
    assert memory.calls[1] == ('repo_map', 'fix retry', 500)
    assert '[ozagia repo map]' in result['context']
    assert 'freshness: fresh' in result['context']
    assert 'pkg/mod.py' in result['context']
    assert 'ozagia_structure' in result['context']
    meta = result['meta']['repo_map']
    assert meta == {
        'delivered': True,
        'tokens': 500,
        'estimated_tokens': meta['estimated_tokens'],
        'files': ['pkg/mod.py', 'tests/test_mod.py'],
        'freshness': 'fresh',
        'graph_hash': 'abc123',
        'cache': {'root_hash': 'abc123', 'cached_files': 2,
                  'parsed': 1, 'reused': 1},
        'truncated': False,
    }
    assert meta['estimated_tokens'] > 0
    stats = state.stats()
    assert stats['repo_map_delivered'] == 1
    assert stats['repo_map_skipped'] == 0
    assert stats['repo_map_tokens'] == meta['estimated_tokens']
    assert stats['repo_map_files'] == 2


def test_repo_map_hint_skips_a_stale_snapshot(tmp_path):
    memory = _RepoMapMemory(snapshot={'freshness': 'stale', 'stale': True})
    state = _repo_map_state(tmp_path, memory)
    result = state.dispatch({'op': 'hint', 'prompt': 'fix retry',
                             'repo_map_tokens': 500})
    assert result['ok'] is True and result['context'] == ''
    assert memory.calls == [('staleness', True)]
    meta = result['meta']['repo_map']
    assert meta['delivered'] is False
    assert meta['reason'] == 'snapshot freshness is stale'
    assert meta['freshness'] == 'stale'
    stats = state.stats()
    assert stats['repo_map_delivered'] == 0
    assert stats['repo_map_skipped'] == 1
    assert stats['repo_map_tokens'] == 0
    assert stats['repo_map_files'] == 0


def test_repo_map_hint_skips_unknown_freshness(tmp_path):
    memory = _RepoMapMemory(snapshot={'stale': False})
    state = _repo_map_state(tmp_path, memory)
    result = state.dispatch({'op': 'hint', 'prompt': 'x', 'repo_map_tokens': 200})
    assert result['ok'] is True and result['context'] == ''
    assert result['meta']['repo_map'] == {
        'delivered': False,
        'reason': 'snapshot freshness is unknown',
        'freshness': 'unknown',
    }
    assert memory.calls == [('staleness', True)]
    assert state.stats()['repo_map_skipped'] == 1


def test_repo_map_hint_skips_an_unverifiable_or_empty_map(tmp_path):
    unavailable = _RepoMapMemory(snapshot={'freshness': 'fresh'})
    unavailable.error = RuntimeError('secret detail')
    state = _repo_map_state(tmp_path, unavailable)
    result = state.dispatch({'op': 'hint', 'prompt': 'x', 'repo_map_tokens': 300})
    meta = result['meta']['repo_map']
    assert meta['delivered'] is False
    assert meta['reason'] == 'repo map unavailable (RuntimeError)'
    assert 'secret detail' not in json.dumps(result)
    assert unavailable.calls == [('staleness', True), ('repo_map', 'x', 300)]

    empty = _RepoMapMemory(map_result={'ok': True, 'markdown': '   ',
                                       'files': []})
    state = _repo_map_state(tmp_path, empty)
    result = state.dispatch({'op': 'hint', 'prompt': 'x', 'repo_map_tokens': 300})
    assert result['ok'] is True and result['context'] == ''
    assert result['meta']['repo_map']['reason'] == 'repo map is empty'
    assert state.stats()['repo_map_skipped'] == 1


def test_repo_map_hint_rejects_an_out_of_range_budget(tmp_path):
    memory = _RepoMapMemory()
    state = _repo_map_state(tmp_path, memory)
    for value in (199, 8001, True, '500', 500.0):
        result = state.dispatch({'op': 'hint', 'prompt': 'x',
                                 'repo_map_tokens': value})
        assert result['ok'] is False and 'repo_map_tokens' in result['error']
    assert memory.calls == []
    assert state.stats()['repo_map_delivered'] == 0
    assert state.stats()['repo_map_skipped'] == 0


def test_repo_map_hint_journal_does_not_store_the_prompt(tmp_path):
    memory = _RepoMapMemory()
    state = _repo_map_state(tmp_path, memory)
    message = {'op': 'hint', 'prompt': 'private task words',
               'repo_map_tokens': 500, 'session': 's1'}
    result = state.dispatch(message)
    state.record(message, result, 1.0)
    raw = (tmp_path / 'journal.jsonl').read_text()
    assert 'private task words' not in raw
    assert json.loads(raw)['op'] == 'hint'
