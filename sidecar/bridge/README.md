# Simli avatar bridge

`simli_bridge.py` gives video calls a face. The sidecar starts it per video call
(`--serve`), sends it the voice model's speech, and gets back H.264 for the call's video
plus Simli's lip-synced copy of the speech, which the call plays so lips and voice match.
The framed stdin/stdout protocol is documented at the top of the script.

Setup: `hermes whatsapp-voice avatar-setup` (private venv with `requirements.txt`), put
`SIMLI_API_KEY` in Hermes's secrets, set `video_avatar: simli`, restart the sidecar.

Offline check (renders a file, no call):

    SIMLI_API_KEY=... python simli_bridge.py --face-id a36d66df-a23d-4569-996b-d0e990019857 \
      --pcm-in speech16k.pcm --h264-out out.h264 --audio-out out.pcm
