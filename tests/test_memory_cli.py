import contextlib
import io
import json
import unittest
from unittest.mock import patch

from renewal.memory_cli import main


class MemoryCliTests(unittest.TestCase):
    def test_recall_is_explicitly_scoped_and_bounded(self):
        output = io.StringIO()
        with patch('renewal.memory_cli.service') as factory, contextlib.redirect_stdout(output):
            factory.return_value.recall.return_value = [{'body': 'Project rollback instructions'}]
            main(['--config', 'private.json', '--namespace', 'personal', '--scope', 'renewal',
                  'recall', '--query', 'rollback', '--budget-chars', '2000'])
        factory.return_value.recall.assert_called_once_with('personal', 'renewal', 'rollback', budget_chars=2000)
        self.assertIn('rollback', json.loads(output.getvalue())[0]['body'])

    def test_write_requires_provenance_and_reads_body_file(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'body.txt'
            path.write_text('Useful project state')
            with patch('renewal.memory_cli.service') as factory, contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.remember.return_value = {'id': 'state'}
                main(['--config', 'private.json', '--namespace', 'personal', '--scope', 'renewal',
                      'remember', '--key', 'state', '--body-file', str(path), '--provenance', 'session:one'])
            args, kwargs = factory.return_value.remember.call_args
            self.assertEqual(args, ('personal', 'renewal', 'state', 'Useful project state'))
            self.assertEqual(kwargs['provenance'], ['session:one'])
