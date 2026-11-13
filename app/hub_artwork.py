"""Download bounded artwork and embed it in the native audio container."""
import base64

from hub_providers import open_audio


def fetch(url):
    if not url:
        return None
    limit = 8 * 1024 * 1024
    with open_audio(url) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('封面图片超过 8 MB')
    if data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):
        return data, 'image/jpeg', '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n') and b'IEND' in data[-12:]:
        return data, 'image/png', '.png'
    raise ValueError('封面接口未返回有效的 JPEG 或 PNG 图片')


def embed(path, artwork):
    import mutagen
    from mutagen.flac import Picture
    from mutagen.id3 import APIC
    from mutagen.mp4 import MP4Cover

    data, mime, _ = artwork
    audio = mutagen.File(path)
    module = type(audio).__module__
    if module == 'mutagen.aac':
        return
    if audio.tags is None:
        audio.add_tags()
    if module in ('mutagen.mp3', 'mutagen.wave'):
        audio.tags.delall('APIC')
        audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc='Cover', data=data))
    elif module == 'mutagen.mp4':
        image_format = MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG
        audio['covr'] = [MP4Cover(data, imageformat=image_format)]
    elif module in ('mutagen.flac', 'mutagen.oggvorbis', 'mutagen.oggopus'):
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = 'Cover'
        picture.data = data
        if module == 'mutagen.flac':
            audio.clear_pictures()
            audio.add_picture(picture)
        else:
            audio['metadata_block_picture'] = [base64.b64encode(picture.write()).decode('ascii')]
    else:
        return
    audio.save()
