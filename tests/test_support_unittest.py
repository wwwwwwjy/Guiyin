"""The published payment assets must be the approved crops retaining the owner's original QR and center logo."""
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import hub_api

ROOT = Path(hub_api.__file__).resolve().parent
OWNER_IMAGES = {
    'support-alipay-qr.png': '33c9f201c7be0fe0c4577ab16fbe33e3dfbb973b5527e63f5461f56d136a8f05',
    'support-wechat-qr.png': '0c6dec96f0d90f4b0f07e1f6d90a060006949f11c5fd9386a82589058b062297',
}
REMOVED_HASHES = {
    'ec847485b2751ed3433456352b84705afaae411ebbf94080c2433c11c12cc67c',
    '975c68cdfa115f6f257ba25a540811ca08a9179e15f6182a2b81356bf0bb51cf',
}

class SupportTests(unittest.TestCase):
    def test_approved_owner_images_match_verified_layout(self):
        for name, digest in OWNER_IMAGES.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((ROOT/'static'/name).read_bytes()).hexdigest(),digest)

    def test_no_legacy_payment_images_even_if_renamed(self):
        for file in (ROOT/'static').rglob('*'):
            if file.is_file():
                self.assertNotIn(hashlib.sha256(file.read_bytes()).hexdigest(),REMOVED_HASHES)

    def test_static_asset_allowlist_and_no_unused_updater(self):
        self.assertEqual({p.name for p in (ROOT/'static').iterdir() if p.is_file()},
                         {'hub-icon.svg','studio.html','studio.js','studio.css',*OWNER_IMAGES})
        for name in ('update_agent.py','update_client.py','version_check.py','hub_support.py'):
            self.assertFalse((ROOT/name).exists())

    def test_payment_dialog_cannot_use_mutable_settings_or_external_images(self):
        js=(ROOT/'static/studio.js').read_text()
        dialog=js.split('function supportDialog()')[1].split('let commentSelectedDirs=')[0]
        self.assertNotIn('api(',dialog)
        self.assertNotIn('https://',dialog)
        for token in ('support-edit','support-form','support.json','loadSupport','saveSupportForm'):
            self.assertNotIn(token,js)
        self.assertNotIn('hub_support',(ROOT/'hub_api.py').read_text())

    def test_payment_images_only_in_click_dialog_with_equal_display_dimensions(self):
        html=(ROOT/'static/studio.html').read_text()
        self.assertNotIn('support-alipay-qr.png',html)
        self.assertNotIn('support-wechat-qr.png',html)
        js=(ROOT/'static/studio.js').read_text()
        self.assertEqual(js.count('width="280" height="280"'),2)
        css=(ROOT/'static/studio.css').read_text()
        self.assertIn('aspect-ratio:1/1',css)
        self.assertIn('object-fit:contain',css)

if __name__ == '__main__':unittest.main()
