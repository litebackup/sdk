"""A Django app that registers continuous backups of SQLite databases on startup.

Add it to INSTALLED_APPS and name the databases to back up::

    INSTALLED_APPS = [..., "litebackup.integrations.django"]
    LITEBACKUP = {
        "databases": {"default": {}},              # or {"default": {"name": "app/main.sqlite"}}
    }

Each database is registered in AppConfig.ready() with ``backups.start()``, which
returns the saved rule when the same settings were registered before, so every
process start may call it. Keys of each entry are backups.start's: ``name``,
``prefix``, and ``maximum_local_databases``. Other top-level keys: ``token``,
``session``, ``socket_path``, and ``binary_path`` for the client, and ``enabled``
(true unless set; turn it off for tests and image builds). The app never shuts
down the daemon. Needs the ``litebackup[django]`` extra.
"""
