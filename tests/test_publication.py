import tempfile
import unittest
import subprocess
from pathlib import Path

from renewal.publication import scan, scan_history


class PublicationTest(unittest.TestCase):
    def test_private_content_and_external_links_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'README.md').write_text('Synthetic project')
            self.assertEqual(scan(root),[])
            (root/'private.txt').write_text('/'+'Users'+'/'+'private-person'+'/workspace\n'+'someone'+'@gmail.com')
            (root/'escape').symlink_to('/private/tmp')
            findings=scan(root)
            self.assertTrue(any(f['reason']=='private local path' for f in findings))
            self.assertTrue(any(f['reason']=='email address' for f in findings))
            self.assertTrue(any(f['reason']=='external or broken symlink' for f in findings))

    def test_credential_file_and_key_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            (root/'.env').write_text('TOKEN=placeholder')
            (root/'example.txt').write_text('sk-'+'x'*45)
            self.assertGreaterEqual(len(scan(root)),2)

    def test_deleted_private_file_remains_detectable_in_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            def git(*args):
                return subprocess.run(['git', *args],cwd=root,check=True,capture_output=True,text=True)
            git('init','-q')
            private=root/'old.txt'
            private.write_text('sk-'+'x'*45)
            git('add','old.txt')
            git('-c','user.name=Synthetic','-c','user.email=synthetic'+'@'+'users.noreply.github.com','commit','-qm','synthetic fixture')
            private.unlink()
            self.assertEqual(scan(root),[])
            self.assertTrue(any(f['reason']=='credential-shaped value' for f in scan_history(root)))
            with self.assertRaises(ValueError):
                scan_history(root/'nested')
