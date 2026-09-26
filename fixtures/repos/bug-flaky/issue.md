unique() sometimes reorders strings

`ordkit.unique` is documented to keep the first occurrence of each item in order. With strings it randomly returns them in a different order: my test passes on some CI runs and fails on others.

```python
from ordkit import unique

print(unique(["b", "a", "b"]))
```

Sometimes this prints `['b', 'a']` (expected), sometimes `['a', 'b']`. Numbers seem fine. ordkit 1.4.0, Python 3.12.
