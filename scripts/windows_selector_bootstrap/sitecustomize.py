"""Use the Windows Selector loop for local PostgreSQL/Alembic subprocess tests.

Set PYTHONPATH to this directory before launching the repository test command.
Python loads sitecustomize in the pytest process and its Alembic children.
"""

import asyncio
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
