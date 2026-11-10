# Simli avatar bridge (WIP, not wired into the Rust sidecar yet)

`simli_bridge.py` streams PCM16 16 kHz mono to Simli (simli-ai 2.1.1), and writes
H.264 Annex-B (constrained baseline, 512x512, 20 fps) plus Simli's lip-synced
PCM16 16 kHz audio. Needs SIMLI_API_KEY in env. Test:

    python simli_bridge.py --face-id a36d66df-a23d-4569-996b-d0e990019857 \
      --pcm-in sample16k.pcm --h264-out out.h264 --audio-out out.pcm \
      --width 512 --height 512 --size 512 --tail 3
