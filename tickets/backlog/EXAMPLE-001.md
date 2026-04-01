# EXAMPLE-001: Add docstring to main module

**Priority:** P3

## Description
Add a module-level docstring to `src/main.py` explaining what the module does.

**Allowed files:**
- `src/main.py`

## Verify:
`python -c "import src.main; assert src.main.__doc__; print('OK')"`
