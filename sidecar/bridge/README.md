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

## Tavus, full pipeline (`tavus_full_bridge.py`, `video_avatar: tavus`)

The Tavus PAL holds the conversation: Tavus speech recognition, turn taking, Raven perception of
the owner's camera, a Tavus-hosted LLM (`tavus-gemma`) steered by the voice model's own prompt and
persona, and the face's own voice, rendered with its lips. The PAL's `ask_hermes` tool comes back
to the sidecar (`0x15`), which runs it on Hermes and answers (`0x07`). The PAL and conversation
are created from `0x06 context` while the phone rings and joined on `0x08 start` (the owner
answered), which makes the face greet. If the face does not come up, the call falls back to the
Gemini voice. Message types are at the top of the script.

## Tavus, echo (`tavus_bridge.py`, `video_avatar: tavus-echo`)

Same wire protocol, with a Tavus Phoenix face in echo mode (persona `pipecat0`): the voice model
keeps the voice and the brain, and Tavus renders a face that shows emotion while it speaks and
listens. Extra messages: `0x04 emotion` (the voice model's `set_expression` tool), `0x05 camera`
(the owner's H.264 camera in), `0x13 snapshot` (a JPEG of it about once a second, which the call
hands to the voice model so it can see the owner).

Setup: `hermes whatsapp-voice avatar-setup`, put `TAVUS_API_KEY` in Hermes's secrets, set
`video_avatar: tavus` or `tavus-echo` (face: `tavus_face_id`, default `rccbeedfc684`), restart the sidecar.
The Tavus conversation is created when the call starts and ended when it ends.
