"""Drop-in directory for your own providers.

Every module in this package is imported at startup and any
:class:`~app.providers.base.StreamProvider` subclass it defines is registered
automatically - no existing file needs editing.

See ``docs/CREATE_PROVIDER.md`` and ``example_provider.py.txt`` in this folder.
"""
