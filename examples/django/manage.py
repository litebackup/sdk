#!/usr/bin/env python
"""Django's command-line utility for the example project."""

import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "project.settings")
    from django.core import management

    management.execute_from_command_line(sys.argv)
