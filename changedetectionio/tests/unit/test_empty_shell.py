#!/usr/bin/env python3
"""The empty-shell detector (changedetectionio/empty_shell.py).

A reply is an empty shell when BOTH its body text length and its body block count are below
the thresholds - the application frame of a JavaScript-built page, which never changes between
checks. A short but real page must not be classified as one.

Run from the tests/ directory:
    python -m unittest unit/test_empty_shell.py
"""
import unittest

from changedetectionio import empty_shell

ROOT_DIV_SHELL = ("<html><head><title>Shop</title><script src='/app.js'></script>"
                  "<link rel='stylesheet' href='/a.css'></head>"
                  "<body><div id='root'></div><noscript>Enable JavaScript</noscript></body></html>")

NEXT_SHELL = ("<html><head><title>Loading</title></head>"
              "<body><div id='__next'></div><div id='app'></div></body></html>")

REAL_PAGE = ("<html><head><title>Shop</title></head><body>"
             "<h1>Product</h1><p>Some description here</p><p>Another paragraph</p></body></html>")


class TestEmptyShell(unittest.TestCase):
    def test_application_shell_is_detected(self):
        self.assertTrue(empty_shell.is_empty_shell(ROOT_DIV_SHELL))
        self.assertTrue(empty_shell.is_empty_shell(NEXT_SHELL))
        self.assertTrue(empty_shell.is_empty_shell('<html><body><div id="root"></div></body></html>'))

    def test_body_with_no_frame_is_not_a_shell(self):
        # Nothing a browser could fill in - that is a failed/empty reply, handled by the empty-page path
        self.assertFalse(empty_shell.is_empty_shell('<html><body></body></html>'))
        self.assertFalse(empty_shell.is_empty_shell('<html><body><!-- a comment only --></body></html>'))

    def test_body_showing_an_image_is_not_a_shell(self):
        # A page showing only an image has content (e.g. a filter that selected just an image)
        self.assertFalse(empty_shell.is_empty_shell('<html><body><img src="something.jpg"></body></html>'))
        self.assertFalse(empty_shell.is_empty_shell('<html><body><div><img src="x.jpg"></div></body></html>'))

    def test_large_body_is_not_parsed_as_a_shell(self):
        # A multi-megabyte body is a rendered page, not an application frame
        page = '<html><body>' + ('<div class="x"></div>' * 50000) + '</body></html>'
        self.assertFalse(empty_shell.is_empty_shell(page))

    def test_script_and_noscript_text_does_not_count(self):
        # JavaScript and "please enable JS" placeholders are the shell's own noise
        self.assertTrue(empty_shell.is_empty_shell(
            "<html><body><script>document.write('lots of words here')</script>"
            "<noscript>Please enable JavaScript to continue</noscript></body></html>"))

    def test_short_but_real_page_is_not_a_shell(self):
        self.assertFalse(empty_shell.is_empty_shell('<html><body>cool it works</body></html>'))
        self.assertFalse(empty_shell.is_empty_shell('<html><body>Hello</body></html>'))
        self.assertFalse(empty_shell.is_empty_shell('<html>test-one</html>'))

    def test_one_long_block_is_content(self):
        long_text = '<html><body><p>' + ('lorem ipsum ' * 40) + '</p></body></html>'
        shape = empty_shell.content_shape(long_text)
        self.assertGreaterEqual(shape[0], 5)
        self.assertFalse(empty_shell.is_empty_shell(long_text))

    def test_two_small_blocks_pass_the_block_threshold(self):
        page = '<html><body><p>ab</p><p>cd</p></body></html>'
        self.assertEqual(empty_shell.content_shape(page), (4, 2))
        self.assertFalse(empty_shell.is_empty_shell(page))

    def test_thresholds_are_both_required(self):
        # Plenty of text but a single block: text threshold is met, so not a shell
        self.assertFalse(empty_shell.is_empty_shell('<html><body><p>' + ('word ' * 30) + '</p></body></html>'))
        # One short block: text is under the threshold and so is the block count, still a shell
        self.assertTrue(empty_shell.is_empty_shell('<html><body><p>a</p></body></html>'))
        # Two short blocks: block count reaches the threshold, so no longer a shell
        self.assertFalse(empty_shell.is_empty_shell('<html><body><p>a</p><p>b</p></body></html>'))


    def test_explicit_threshold_arguments(self):
        page = '<html><body><div>hello there</div></body></html>'
        self.assertFalse(empty_shell.is_empty_shell(page))
        self.assertTrue(empty_shell.is_empty_shell(page, min_text=100, min_blocks=2))

    def test_shape_of_empty_input(self):
        self.assertEqual(empty_shell.content_shape(''), (0, 0))
        self.assertEqual(empty_shell.content_shape(None), (0, 0))

    def test_malformed_markup_does_not_raise(self):
        # Never fail a check because the shape could not be measured
        self.assertIsInstance(empty_shell.is_empty_shell('<html><body><div'), bool)


if __name__ == '__main__':
    unittest.main()
