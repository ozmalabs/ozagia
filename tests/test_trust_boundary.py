from pathlib import Path

from ozagia.core.trust_boundary import analyze_file, analyze_source


def test_tracks_request_value_through_local_assignment_to_sql_sink() -> None:
    findings = analyze_source('''
def handler(request):
    value = request.args.get("name")
    query = "select * from users where name=" + value
    db.execute(query)
''', file="app.py")
    assert len(findings) == 1
    assert findings[0]["rule"] == "trust-flow-sql"
    assert findings[0]["source_line"] == 3
    assert findings[0]["line"] == 5
    assert findings[0]["confidence"] == "low"


def test_ignores_untainted_sink_and_does_not_cross_function_boundary() -> None:
    findings = analyze_source('''
def read(request):
    value = request.args.get("name")
def write():
    db.execute("select 1")
    db.execute(value)
''')
    assert findings == []


def test_handles_direct_source_and_unparseable_source() -> None:
    findings = analyze_source('subprocess.run(input(), shell=True)')
    assert len(findings) == 1
    assert findings[0]["category"] == "command"
    assert analyze_source("def broken(:") == []


def test_file_scan_is_root_relative_and_bounded(tmp_path: Path) -> None:
    source = tmp_path / "app.py"
    source.write_text("def f(request):\n    open(request.args.get('p'))\n")
    got = analyze_file(source, root=tmp_path)
    assert got[0]["file"] == "app.py"
    source.write_text("x" * 600_000)
    assert analyze_file(source, root=tmp_path) == []


def test_get_methods_need_request_or_http_context() -> None:
    benign = analyze_source('''
def f(mapping, cache, value):
    result = mapping.get("fixed")
    cached = cache.get(value)
    execute(cached)
''')
    assert benign == []

    request_flow = analyze_source('''
def f(request):
    value = request.args.get("url")
    requests.get(value)
''')
    assert [(item["category"], item["source_line"], item["line"])
            for item in request_flow] == [("ssrf", 3, 4)]

    known_clients = analyze_source('''
def f(request):
    value = request.args.get("url")
    session = requests.Session()
    session.get(value)
    client = httpx.Client()
    client.post(value)
''')
    assert [(item["category"], item["source_line"], item["line"])
            for item in known_clients] == [("ssrf", 3, 5), ("ssrf", 3, 7)]

    ambiguous_clients = analyze_source('''
def f(request):
    value = request.args.get("id")
    db.client.get(value)
    session.get(value)
    session = database.session()
    session.get(value)
''')
    assert ambiguous_clients == []
