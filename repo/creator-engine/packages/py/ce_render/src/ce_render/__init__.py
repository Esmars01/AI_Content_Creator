"""Timeline/EDL, FFmpeg filtergraph compiler, captions placement and the audio mix (§27).

Status: implemented and tested in Phase 2: the timeline resolves every shot, cue and SFX from
anchors against the aligned dialogue; the mix places the dialogue stem, ducks music beds with an
exact gain envelope, adds SFX and runs two-pass loudnorm (verified with ebur128); the compositor
cuts the base track, composites overlays, draws title cards, burns ASS captions and provenance
labels and encodes per render preset; the proxy is 540p. Procedural camera motion, reframing and
the realism post land in Phase 7 (`ce_camera`, `ce_realism`).
"""

__version__ = "0.1.0"
IMPLEMENTED_IN_PHASE = 2
