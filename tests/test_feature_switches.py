"""Install-level switches that were documented, reported by doctor.py, and read by nobody."""
from types import SimpleNamespace

from bot import MaxwellBot


def _bot(**cfg):
    base = {"ENABLE_IMAGE_INPUT": True, "ENABLE_TTS_VC": True, "ENABLE_AUTONOMY": True}
    base.update(cfg)
    return SimpleNamespace(config=SimpleNamespace(**base), _control={})


def test_image_input_needs_both_switches():
    # .env is the hard switch; the dashboard control is the soft one. Only the
    # soft one was ever read, so ENABLE_IMAGE_INPUT=false forwarded images
    # anyway — the opposite of what someone turning it off for cost or privacy
    # expects.
    assert MaxwellBot._image_input_enabled(_bot()) is True

    off_in_env = _bot(ENABLE_IMAGE_INPUT=False)
    assert MaxwellBot._image_input_enabled(off_in_env) is False

    off_in_dashboard = _bot()
    off_in_dashboard._control = {"process_images": False}
    assert MaxwellBot._image_input_enabled(off_in_dashboard) is False


def test_image_input_defaults_on_when_unset():
    bare = SimpleNamespace(config=SimpleNamespace(), _control={})
    assert MaxwellBot._image_input_enabled(bare) is True
