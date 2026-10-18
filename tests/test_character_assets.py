"""Each selection must switch all identity-dependent assets together."""
from pathlib import Path
import pytest
from feathertalk_lab.character_assets import character_assets

def test_default_preserves_gentleman(monkeypatch):
    monkeypatch.delenv('FEATHERTALK_CHARACTER',raising=False)
    assets=character_assets()
    assert assets.checkpoint==Path('/work/checkpoints/retrain_20260929/best.pth')
    assert assets.image==Path('/work/data/base.jpg')
    assert assets.base==Path('/work/data/loop_base')
    assert assets.teeth==Path('/work/data/teeth_20260930')
    assert assets.detail==Path('/work/data/detail_balance')

@pytest.mark.parametrize('selection',['QinChe','qinche',' QINCHE '])
def test_qinche_identity_bundle(selection):
    with pytest.raises(RuntimeError,match='independent approved runtime'):
        character_assets(selection)

def test_environment_selection_and_invalid_value(monkeypatch):
    monkeypatch.setenv('FEATHERTALK_CHARACTER','QinChe')
    with pytest.raises(RuntimeError,match='independent approved runtime'):
        character_assets()
    with pytest.raises(ValueError,match='SPECTER_AVATAR_CHARACTER'):
        character_assets('unknown')

def test_rejected_qinche_bundle_cannot_be_loaded(monkeypatch):
    original=Path.is_file
    monkeypatch.setattr(Path,'is_file',lambda p:True if p.name=='REJECTED_20261016.json' else original(p))
    with pytest.raises(RuntimeError,match='independent approved runtime'):
        character_assets('QinChe')
    assert character_assets('gentleman').key=='gentleman'
