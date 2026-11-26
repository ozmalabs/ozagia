# `ozagia-console`

> **Generated file.** Built by `scripts/docs/generate_docs.py` from `src/ozagia/console.py` (ozagia `0.1.5.dev0`). Do not edit by hand: change the source code and regenerate.

Source: `src/ozagia/console.py` (`ozagia-console` entry point).

# `ozagia-console`

serve a loopback-only, read-only ozagia console

```
usage: ozagia-console [-h] [--root ROOT] [--host HOST] [--port PORT] [--open]
                      [--token TOKEN] [--allow-remote]
```

| option | type | values | default | help |
|---|---|---|---|---|
| `--root` |  |  |  | repository root (default: the current checkout) |
| `--host` |  |  | `127.0.0.1` | bind host (default: 127.0.0.1; loopback only) |
| `--port` | int |  | 0 | bind port (default: 0, an ephemeral port) |
| `--open` |  |  |  | open the token URL in the default browser |
| `--token` |  |  |  | explicit URL-safe token (default: a random token) |
| `--allow-remote` |  |  |  | allow a non-loopback bind; requires --token (not recommended) |
