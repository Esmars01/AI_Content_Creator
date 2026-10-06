"""audio.emotion — emotion2vec+ base (§16.1 `audio_emotion`, Phase 11). Sandbox only: the model is
published under the FunASR Model Open Source License, which permits use with attribution but says
the weights are "provided for reference and learning purposes only"; commercial use is not
verified, so the manifest records `commercial_use: false` and the engine never leaves the sandbox
until the owner decides (§41). Coarse classes only; their mapping to the canonical emotion labels
lives in `config/vocab/observation_proxies.yaml` (§16.8: audio emotion classes are coarse)."""

from pathlib import Path

PLUGIN_MANIFEST = Path(__file__).with_name("plugin.yaml")
