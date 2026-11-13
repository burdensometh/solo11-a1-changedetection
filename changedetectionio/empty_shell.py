"""Detect a page that came back as an "empty shell".

Some sites answer a plain HTTP fetch with the application frame only - a `<body>` holding
`<div id="root"></div>` (or similar) plus a pile of scripts - while the real content is only
put there by JavaScript. Nothing in that reply changes from check to check, so the monitor
looks healthy and never records a change, and an actual failed fetch looks exactly the same.

The tell is the shape of the content, measured after script/style/head noise is removed:
how much body text there is ("body text length") and how many elements carry body text
("block count"). A reply where BOTH are below their thresholds is an empty shell: it is
worth re-fetching once with a browser, and if it is still empty it is a failure, not a
change (see processors/base.py and worker.py).

The defaults are deliberately conservative so a short but real page ("cool it works") is
not mistaken for a shell. They can be tuned per install:

  EMPTY_SHELL_MIN_TEXT_LENGTH   body characters (whitespace collapsed), default 5
  EMPTY_SHELL_MIN_BLOCK_COUNT   text-carrying elements in the body, default 2

The reply must also be a complete document whose body holds something a browser could
render into (an element or a script), and must not show an image/video on its own. A blank
`<html><body></body></html>`, or one that only has an `<img>`, keeps the existing
"no usable text" handling instead of being called a shell.
"""

import html as _html
import os
import re

from bs4 import BeautifulSoup, Comment, NavigableString
from loguru import logger

# Elements that never render as body text for the purpose of this check. `<noscript>` is
# included because SPA shells put "Please enable JavaScript" there on purpose.
_INVISIBLE_TAGS = ['script', 'style', 'noscript', 'template', 'svg', 'math', 'canvas', 'iframe', 'head']

# Elements that are content in themselves - a page whose body holds one of these is showing
# something even without text (e.g. a watch whose filter selected just an image), so it is
# not an empty shell that a browser retry would fill in.
_CONTENT_ELEMENTS_TAGS = ['img', 'video', 'audio', 'picture', 'object', 'embed', 'iframe']

_BODY_RE = re.compile(r'<body\b[^>]*>(.*?)(?:</body\s*>|$)', re.I | re.S)
_INVISIBLE_BLOCK_RE = re.compile(r'<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>', re.I | re.S)
_COMMENT_RE = re.compile(r'<!--.*?-->', re.S)
_TAG_RE = re.compile(r'<[^>]+>')
_WHITESPACE_RE = re.compile(r'\s+')

# The fast text pass only needs to know whether there is a little text, so it scans a bounded
# prefix of the body rather than a whole multi-megabyte page (a real page has plenty of text
# early on; a shell has none anywhere, and then the parser pass below looks at everything).
_FAST_PASS_SCAN_CHARS = 200_000

# A shell's body is a small frame (a root element, maybe a bootstrap blob). A body with a lot
# of markup in it is not one, and is not worth parsing just to count its text - this keeps the
# check cheap on multi-megabyte real pages.
_MAX_BODY_MARKUP_CHARS = 50_000

MIN_TEXT_LENGTH_DEFAULT = 5
MIN_BLOCK_COUNT_DEFAULT = 2


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        logger.warning(f"Ignoring invalid {name}={os.getenv(name)!r}, using {default}")
        return default


def min_text_length():
    return _env_int('EMPTY_SHELL_MIN_TEXT_LENGTH', MIN_TEXT_LENGTH_DEFAULT)


def min_block_count():
    return _env_int('EMPTY_SHELL_MIN_BLOCK_COUNT', MIN_BLOCK_COUNT_DEFAULT)


def _body_fragment(html_content, limit=None):
    """The <body>...</body> slice, or the whole document when there is no body tag."""
    match = _BODY_RE.search(html_content)
    fragment = match.group(1) if match else html_content
    return fragment[:limit] if limit else fragment


def _fast_body_text_length(html_content):
    """Whitespace-collapsed length of the body text, without a costly parser walk.

    Only used as a first pass: when it already reaches the threshold there is nothing
    to count, so real (large) pages never pay for the BeautifulSoup pass below. Measuring
    a bounded prefix is enough for that decision - a page with real text has some early on.
    """
    body = _body_fragment(html_content, limit=_FAST_PASS_SCAN_CHARS)
    body = _INVISIBLE_BLOCK_RE.sub(' ', body)
    body = _COMMENT_RE.sub(' ', body)
    body = _TAG_RE.sub(' ', body)
    body = _html.unescape(body)
    return len(_WHITESPACE_RE.sub(' ', body).strip())


def _is_text_node(node):
    return isinstance(node, NavigableString) and not isinstance(node, Comment) and bool(str(node).strip())


def _body_root(html_content):
    soup = BeautifulSoup(_body_fragment(html_content), 'html.parser')
    for tag in soup.find_all(_INVISIBLE_TAGS):
        tag.decompose()
    return soup.body or soup


def content_shape(html_content, text_length_enough=None):
    """(body_text_length, body_block_count) for an HTML document.

    Whitespace is collapsed before measuring, so indentation does not count as content.
    `text_length_enough`: when the measured text reaches this, block counting is skipped
    (both have to be under their thresholds for a shell) and the block count is None.
    """
    if not html_content or not isinstance(html_content, str):
        return 0, 0

    try:
        text_length = _fast_body_text_length(html_content)
        if text_length_enough is not None and text_length >= text_length_enough:
            return text_length, None

        root = _body_root(html_content)
        # Re-measure on the parsed body: the fast pass cannot see through every nested tag
        text_length = len(_WHITESPACE_RE.sub(' ', ''.join(
            str(s) for s in root.find_all(string=True) if _is_text_node(s))).strip())

        # A "block" is an element that directly carries non-whitespace text, or direct text
        # under the body itself. Wrappers around such elements do not count twice.
        block_count = 0
        for element in root.find_all(True):
            if any(_is_text_node(child) for child in element.contents):
                block_count += 1
        if any(_is_text_node(child) for child in root.contents):
            block_count += 1
    except Exception as e:
        # Never fail a check because the shape could not be measured
        logger.warning(f"Could not measure page body shape: {e}")
        return 0, 0

    return text_length, block_count


def is_empty_shell(html_content, min_text=None, min_blocks=None):
    """True when the body has a frame (elements or scripts) but no content in it.

    Both measures must be below their thresholds - plenty of text, or enough text-carrying
    elements, is content. A body with no element and no script at all is not a shell: there
    is no frame that a browser could fill in, it is simply an empty/failed reply (the
    existing empty-page handling covers that). A body holding only an image/video element
    is showing something, so it is content, not a shell.
    """
    min_text = min_text_length() if min_text is None else min_text
    min_blocks = min_block_count() if min_blocks is None else min_blocks

    if not html_content or not isinstance(html_content, str):
        return False
    body = _body_fragment(html_content)
    if len(body) > _MAX_BODY_MARKUP_CHARS:
        # A small frame, not a large rendered page - no need to parse it
        return False
    text_length, block_count = content_shape(html_content, text_length_enough=min_text)
    if block_count is None:
        return False
    return (text_length < min_text
            and block_count < min_blocks
            and has_body_scaffolding(html_content)
            and not has_media_element(html_content))


def has_body_scaffolding(html_content):
    """True when the body holds something a browser could render into: an element or a script.

    An application shell is `<body><div id="root"></div><script ...>` - frame present, no text.
    `<html><body></body></html>` has no frame, and retrying it with a browser cannot help. The
    page must also be a complete document with a body, so a fragment (or a document missing its
    body) is left to the existing empty-page handling rather than judged as a shell.
    """
    if not html_content or not isinstance(html_content, str):
        return False
    if not _BODY_RE.search(html_content):
        return False
    try:
        soup = BeautifulSoup(_body_fragment(html_content), 'html.parser')
        return any(True for _ in soup.find_all(True))
    except Exception as e:
        logger.warning(f"Could not inspect page body scaffolding: {e}")
        return False


def has_media_element(html_content):
    """True when the body holds an image/video/embedding - that is something to show, not a shell."""
    if not html_content or not isinstance(html_content, str):
        return False
    try:
        soup = BeautifulSoup(_body_fragment(html_content), 'html.parser')
        return bool(soup.find(_CONTENT_ELEMENTS_TAGS))
    except Exception as e:
        logger.warning(f"Could not inspect page body for media elements: {e}")
        return False
