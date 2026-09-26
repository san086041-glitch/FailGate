slugify produces repeated dashes since 0.2.0

After upgrading slugkit from 0.1.x to 0.2.0, titles with punctuation followed by spaces get several dashes in a row:

```python
>>> from slugkit import slugify
>>> slugify("Hello,  World!")
'hello---world'
```

In 0.1.x this returned `'hello-world'`, which is what I expect: runs of spaces and punctuation should become a single separator. Our URLs changed after the upgrade.
