# Avatar bridges

`simli_bridge.py` gives video calls a face. The sidecar starts it per video call
(`--serve`), sends it the voice model's speech, and gets back H.264 for the call's video
plus Simli's lip-synced copy of the speech, which the call plays so lips and voice match.
The framed stdin/stdout protocol is documented at the top of the script.

Setup: `hermes whatsapp-voice avatar-setup` (private venv with `requirements.txt`), put
`SIMLI_API_KEY` in Hermes's secrets, set `video_avatar: simli`, restart the sidecar.

Offline check (renders a file, no call):

    SIMLI_API_KEY=... python simli_bridge.py --face-id a36d66df-a23d-4569-996b-d0e990019857 \
      --pcm-in speech16k.pcm --h264-out out.h264 --audio-out out.pcm

## Tavus (`tavus_bridge.py`)

Same wire protocol, with a Tavus Phoenix face in echo mode (persona `pipecat0`): the voice model
keeps the voice and the brain, and Tavus renders a face that shows emotion while it speaks and
listens. Extra messages: `0x04 emotion` (the voice model's `set_expression` tool), `0x05 camera`
(the owner's H.264 camera in), `0x13 snapshot` (a JPEG of it about once a second, which the call
hands to the voice model so it can see the owner).

Setup: `hermes whatsapp-voice avatar-setup`, put `TAVUS_API_KEY` in Hermes's secrets, set
`video_avatar: tavus` (face: `tavus_face_id`, default `r3f4182ef554`), restart the sidecar.
The Tavus conversation is created when the call starts and ended when it ends.
