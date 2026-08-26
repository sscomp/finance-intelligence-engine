"""reports.hermes package marker.

Phase 2B Step 4A. This subpackage holds the Dispatcher / Task
framework skeleton. It is intentionally NOT wired into the existing
Hermes cronjob scheduler — production scheduling remains the
cronjob's job. This package provides the Python-side abstractions
(task model, status machine, dispatcher API, CLI) so that Phase 2C
can replace the bash `run*.sh` wrappers with `dispatcher.dispatch()`
calls without rewriting the existing cronjob entries.
"""
