"""Stage 1 test package.

This file must exist. Measured on Python 3.10.11: without it,
`python -m unittest discover -s tests -t . -v` fails outright with
`ImportError: Start directory is not importable`.

Worse, the obvious workaround `python -m unittest discover -s . -p "test_*.py" -v`
exits 0 after running ZERO tests. That is a silent false green, which is the one
failure mode this package exists to prevent.
"""