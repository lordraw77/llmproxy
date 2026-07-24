"""Startup banner.

Renders an ASCII-art "LLMPROXY" banner (block-letter font) followed by the
version line and the project URL, in the spirit of other self-hosted LLM tools.
The banner is emitted once, at process startup, by the development entrypoint.
"""

PROJECT_URL = "https://github.com/lordraw77/llmproxy"

# Each glyph is five rows tall; rows are joined column-wise to build a line.
_GLYPHS = {
    "L": (" _     ", "| |    ", "| |    ", "| |___ ", "|_____|"),
    "M": (" __  __ ", "|  \\/  |", "| |\\/| |", "| |  | |", "|_|  |_|"),
    "P": (" ____  ", "|  _ \\ ", "| |_) |", "|  __/ ", "|_|    "),
    "R": (" ____  ", "|  _ \\ ", "| |_) |", "|  _ < ", "|_| \\_\\"),
    "O": ("  ___  ", " / _ \\ ", "| | | |", "| |_| |", " \\___/ "),
    "X": ("__  __", "\\ \\/ /", " \\  / ", " /  \\ ", "/_/\\_\\"),
    "Y": ("__   __", "\\ \\ / /", " \\ V / ", "  | |  ", "  |_|  "),
}

_WORD = "LLMPROXY"


def _render_art():
    """Return the multi-line ASCII-art title as a single string."""
    rows = ["".join(_GLYPHS[ch][r] for ch in _WORD) for r in range(5)]
    return "\n".join(rows)


def render_banner(version="dev"):
    """Return the full startup banner (art + version line + URL) as a string.

    Args:
        version: Version string to display (e.g. ``"1.1.0"``).

    Returns:
        The banner text, without a trailing newline.
    """
    return (
        "\n"
        f"{_render_art()}\n\n"
        f"v{version} - building a fast, multi-endpoint LLM proxy.\n\n"
        f"{PROJECT_URL}\n"
    )
