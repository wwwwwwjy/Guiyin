"""Audio integrity policy inspired by jia070310/lemon-muisc (MIT).

Uses Mutagen's parsed codec rather than a resolver label or HTTP Content-Type.
This detects container/codec mismatch, not an MP3 transcoded into real FLAC.
"""

def validate_quality(audio, quality):
    if str(quality or '').lower() not in ('lossless', 'hires', 'flac', 'flac24bit', 'master'):
        return
    module = type(audio).__module__
    if module == 'mutagen.flac':
        return
    if module == 'mutagen.mp4' and getattr(audio.info, 'codec', '') == 'alac':
        return
    raise ValueError('音源声明无损，但实际音频编码不是无损；已拒绝保存并尝试其他来源')
