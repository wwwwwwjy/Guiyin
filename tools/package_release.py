"""Audit the exact distribution inputs, then build a reproducible source zip."""
import argparse
import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OWNER_IMAGES = {
    'support-alipay-qr.png': '33c9f201c7be0fe0c4577ab16fbe33e3dfbb973b5527e63f5461f56d136a8f05',
    'support-wechat-qr.png': '0c6dec96f0d90f4b0f07e1f6d90a060006949f11c5fd9386a82589058b062297',
}
ALLOWED_STATIC = {'hub-icon.svg', 'studio.html', 'studio.css', 'studio.js', *OWNER_IMAGES}
REMOVED_HASHES = {
    'ec847485b2751ed3433456352b84705afaae411ebbf94080c2433c11c12cc67c',
    '975c68cdfa115f6f257ba25a540811ca08a9179e15f6182a2b81356bf0bb51cf',
}
REMOVED_PATHS = {'app/update_agent.py', 'app/update_client.py', 'app/version_check.py',
                 'app/requirements.txt', 'app/VERSION'}
ROOT_FILES = {'README.md', 'THIRD_PARTY_NOTICES.md', 'LICENSE', 'Dockerfile',
              '.dockerignore', '.gitignore', '.env.example', 'requirements.txt',
              'compose.yaml', 'compose.build.yaml', 'compose.local.yaml', 'CHANGELOG.md', 'CONTRIBUTING.md'}
DIRECTORIES = {'app', 'tests', 'licenses', 'third_party', 'tools', 'docs', '.github'}


def audit_files(root=ROOT):
    files = []
    errors = []
    screenshot_manifest = root / 'docs/screenshots/manifest.json'
    screenshots = json.loads(screenshot_manifest.read_text()) if screenshot_manifest.exists() else {}
    for p in root.rglob('*'):
        rel = p.relative_to(root)
        if any(part in ('__pycache__', '.venv', '.git', '.pytest_cache', 'node_modules') for part in rel.parts):
            continue
        if p.is_symlink():
            errors.append(f'发布目录不允许符号链接: {rel}')
            continue
        if not p.is_file():
            continue
        if rel.as_posix() in REMOVED_PATHS or rel.parts[0] == 'research':
            errors.append(f'存在已移除的上游资源: {rel}')
        if rel.parts[0] not in DIRECTORIES and rel.as_posix() not in ROOT_FILES:
            continue
        if p.suffix == '.pyc':
            continue
        if rel.parts[:2] == ('app', 'static') and rel.as_posix() not in {'app/static/' + n for n in ALLOWED_STATIC}:
            errors.append(f'未批准的前端静态资源: {rel}')
        raw = p.read_bytes()
        if hashlib.sha256(raw).hexdigest() in REMOVED_HASHES:
            errors.append(f'存在已移除的第三方收款码: {rel}')
        if p.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif', '.webp'):
            expected = OWNER_IMAGES.get(p.name) if rel.parts[:2] == ('app', 'static') else None
            if rel.parts[:2] == ('docs', 'screenshots'):
                expected = screenshots.get(p.name)
            if not expected or hashlib.sha256(raw).hexdigest() != expected:
                errors.append(f'图片不在项目作者批准的文件指纹清单内: {rel}')
        if p.name in ('support.json', 'admin.password', '.env'):
            errors.append(f'不得打包部署者私人配置: {rel}')
        if p.suffix == '.gz':
            with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
                for member in archive.getmembers():
                    if member.isfile():
                        payload = archive.extractfile(member).read()
                        if hashlib.sha256(payload).hexdigest() in REMOVED_HASHES:
                            errors.append(f'依赖包含已移除收款码: {rel}:{member.name}')
        files.append(p)
    for name in OWNER_IMAGES:
        if not (root / 'app/static' / name).is_file():
            errors.append(f'缺少项目作者收款码: {name}')
    if errors:
        raise ValueError('\n'.join(errors))
    return sorted(files)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audit-only', action='store_true')
    args = parser.parse_args()
    files = audit_files()
    version = (ROOT / 'app/HUB_VERSION').read_text().strip()
    manifest = {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    if args.audit_only:
        print(f'Audit passed: {len(files)} approved files; owner-approved payment images and manifest-pinned screenshots, no obsolete updater.')
        return
    target = ROOT.parent / f'Guiyin-{version}.zip'
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for p in files:
            archive.write(p, 'music-hub/' + p.relative_to(ROOT).as_posix())
        archive.writestr('music-hub/SHA256SUMS.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'path': str(target), 'files': len(files) + 1,
                      'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
