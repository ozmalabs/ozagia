"""Agent harnesses: run a whole agent session on a repository with ozagia in its path.

- `ozagia.harness.session`: `GraphSession`, a current snapshot and a running graph daemon for one repository.
- `ozagia.harness.opencode`: OpenCode, with the capture plugin and the graph daemon (`ozagia agent`).
- `ozagia.harness.native`: a small agent loop over an OpenAI-compatible API, with the intent layer in process.
"""
