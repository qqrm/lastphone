# Synthetic video fixture

`video.mp4` is a 14-second, 160 × 90 H.264/AAC file made for Lastphone tests:
a dark-green frame and a 440 Hz sine wave. It contains no third-party footage.
Its SHA-256 is pinned in `make_fixture.py`. It is included only in the test APK.

The integration test checks exact HTTP byte ranges, real WebView media metadata,
and clicking the caption at 12 seconds to seek in this file. Keeping the small
fixture in Git removes a large system-package download from every CI run.

To regenerate deliberately with ffmpeg, use:

```bash
ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i 'color=c=darkgreen:s=160x90:r=10' \
  -f lavfi -i 'sine=frequency=440:sample_rate=44100' \
  -t 14 -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart \
  -metadata 'title=Lastphone synthetic fixture' tools/android/fixtures/video.mp4
```

Review the resulting format and update the checksum together with the fixture.
Different ffmpeg versions need not produce byte-identical encoding.
