from ozagia.core.deep_review.analyzers.failure_modes import analyze


def test_detects_only_explicit_unconditional_exception_retry():
    source = '''\
def fetch():
    while True:
        try:
            request()
        except OSError:
            continue
'''
    findings = analyze("src/client.py", source)
    assert len(findings) == 1
    finding = findings[0]
    assert finding["section"] == "failure-modes"
    assert finding["type"] == "unbounded_exception_retry"
    assert finding["line"] == 2
    assert finding["locations"] == [{"path": "src/client.py", "symbol": "fetch"}]
    assert finding["stable_key"].startswith("static:src/client.py:")


def test_ignores_bounded_or_unrelated_retry_shapes():
    source = '''\
def bounded(attempts):
    for attempt in range(attempts):
        try:
            request()
        except OSError:
            continue

def unrelated():
    while True:
        try:
            request()
        except OSError:
            break

def inner_loop_only():
    while True:
        while pending():
            try:
                request()
            except OSError:
                continue
'''
    assert analyze("client.py", source) == []


def test_invalid_python_returns_no_candidates():
    assert analyze("broken.py", "while True: try") == []


def test_flags_broad_handler_returning_default_but_not_narrow_error_handling():
    source = '''\
def load():
    try:
        return read_config()
    except Exception:
        return {}

def expected_missing_file():
    try:
        return read_optional()
    except FileNotFoundError:
        return None
'''
    findings = analyze("config.py", source)
    assert [finding["type"] for finding in findings] == [
        "broad_exception_returns_default"]
    assert findings[0]["locations"] == [{"path": "config.py", "symbol": "load"}]
    assert findings[0]["line"] == 4


def test_ignores_shadowed_broad_exception_names_and_nonliteral_containers():
    source = '''\
Exception = ValueError

def shadowed():
    try:
        work()
    except Exception:
        return {}

def computed_fallback():
    try:
        work()
    except BaseException:
        return [make_default()]
'''
    assert analyze("fallbacks.py", source) == []


def test_shadowing_is_limited_to_the_lexical_scope():
    source = '''\
def shadowed_locally():
    Exception = ValueError
    try:
        work()
    except Exception:
        return {}

def unrelated_builtin_handler():
    try:
        work()
    except Exception:
        return {}

def nested_shadow_does_not_hide_outer_handler():
    def helper():
        Exception = ValueError
    try:
        work()
    except Exception:
        return {}
'''
    findings = analyze("fallbacks.py", source)
    assert [item["locations"][0]["symbol"] for item in findings] == [
        "unrelated_builtin_handler", "nested_shadow_does_not_hide_outer_handler"]


def test_required_keyword_only_parameter_shadows_exception_name():
    source = '''\
def fallback(*, Exception):
    try:
        work()
    except Exception:
        return {}
'''
    assert analyze("fallbacks.py", source) == []


def test_enclosing_function_binding_shadows_nested_handler():
    source = '''\
def outer(BaseException):
    def inner():
        try:
            work()
        except BaseException:
            return {}
    return inner
'''
    assert analyze("fallbacks.py", source) == []


def test_accepts_container_with_only_nested_literals():
    findings = analyze("fallbacks.py", '''\
def fallback():
    try:
        work()
    except BaseException:
        return {"items": [None, 3]}
''')
    assert [finding["type"] for finding in findings] == [
        "broad_exception_returns_default"]


def test_candidate_flows_through_bounded_static_file_scan(tmp_path):
    from ozagia.core.deep_review.analyzers.static import analyze as analyze_static

    (tmp_path / "worker.py").write_text(
        "def work():\n    while True:\n        try:\n            run()\n"
        "        except OSError:\n            continue\n", encoding="utf-8")
    findings, checked, skipped = analyze_static(tmp_path)
    result = next(item for item in findings
                  if item["type"] == "unbounded_exception_retry")
    assert checked == ["worker.py"]
    assert skipped == []
    assert result["locations"] == [{"path": "worker.py", "symbol": "work"}]
    assert result["stable_key"] == (
        "static:worker.py:unbounded_exception_retry:work:1")
