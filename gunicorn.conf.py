"""Gunicorn configuration for llmproxy.

Referenced explicitly from the Docker image CMD (``gunicorn -c gunicorn.conf.py``).
Its only job is the ``on_starting`` server hook, which prints the startup banner
once in the master process — the development server prints it from ``main.main()``,
so this covers the production (gunicorn) path.

Runtime tuning (workers, threads, timeout, bind) stays on the command line so it
can be driven by environment variables in the image.
"""

from llmproxy import __version__
from llmproxy.banner import render_banner


def on_starting(server):
    """Print the ASCII startup banner once, in the gunicorn master process."""
    print(render_banner(__version__), flush=True)
