import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hub
from message_format import blocks, clean_reply, inline_markup, terminal_reply

class MessageFormatTests(unittest.TestCase):
    def test_reasoning_cleanup_preserves_literal_code(self):
        text = '<think>private reasoning</think>\n## Result\nDone.\n```xml\n<think>literal</think>\n```'
        result = clean_reply(text)
        self.assertNotIn('private reasoning', result)
        self.assertIn('<think>literal</think>', result)
        self.assertEqual(clean_reply('<think>unfinished'), '')

    def test_blocks_keep_code_and_make_headings_and_lists_readable(self):
        result = blocks('## Changes\n\n- Added **checks**\n- Fixed `total()`\n\n```python\nprint("ok")\n```')
        self.assertEqual([k for k,v in result], ['heading','list','list','code'])
        self.assertEqual(result[-1][1], 'print("ok")')
        self.assertIn('•', result[1][1])

    def test_markup_cannot_inject_native_ui_attributes(self):
        value = inline_markup('<span size="huge">bad</span> **safe & sound** `a < b`')
        self.assertNotIn('<span', value)
        self.assertIn('&lt;span', value)
        self.assertIn('<b>safe &amp; sound</b>', value)
        self.assertIn('<tt>a &lt; b</tt>', value)

    def test_terminal_answer_is_readable_without_control_codes_when_redirected(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            terminal_reply('## Checks\n- **Passed** tests\n```python\nprint(1)\n```')
        self.assertNotIn('\x1b', out.getvalue())
        self.assertNotIn('**', out.getvalue())
        self.assertIn('    print(1)', out.getvalue())

    def test_explicit_free_model_still_requires_live_zero_price_verification(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(hub,'refresh_free_models',return_value=[]):
            with self.assertRaisesRegex(ValueError,'verified as free'):
                hub.run_task(temp,'task',backend='free',model='space-bunny-free',dry_run=True)
        with tempfile.TemporaryDirectory() as temp, patch.object(hub,'local_models',return_value=[]):
            with self.assertRaisesRegex(ValueError,'not installed'):
                hub.run_task(temp,'task',backend='local',model='missing',dry_run=True)
