# Repository-root conftest. Tests of libs/confkit run with libs/confkit/pyproject.toml
# as their config file, so pytest must never load this file for them.
raise RuntimeError("root conftest.py was loaded: pytest is not using the subdir's config")
