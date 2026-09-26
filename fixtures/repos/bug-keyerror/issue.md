Parsing a config crashes when it has keys before the first section

I'm on confkit 0.3.0. The docs say keys that appear before the first `[section]` go into `DEFAULT`, but parsing such a file crashes:

```python
import confkit

confkit.parse("name = demo\n[server]\nhost = example.org\n")
```

```
Traceback (most recent call last):
  File "/home/me/app/load.py", line 3, in <module>
    confkit.parse("name = demo\n[server]\nhost = example.org\n")
  File "/home/me/.venv/lib/python3.12/site-packages/confkit/parser.py", line 27, in parse
    _store(sections, current, line)
  File "/home/me/.venv/lib/python3.12/site-packages/confkit/parser.py", line 35, in _store
    sections[section][key.strip()] = value.strip()
KeyError: None
```

Expected: `{"DEFAULT": {"name": "demo"}, "server": {"host": "example.org"}}`.
