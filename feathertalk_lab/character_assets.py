"""Select one complete identity-specific asset bundle before model loading."""
from dataclasses import dataclass
from pathlib import Path
import os

@dataclass(frozen=True)
class CharacterAssets:
    key: str
    name: str
    checkpoint: Path
    data: Path
    @property
    def base(self):return self.data/'loop_base'
    @property
    def teeth(self):return self.data/'teeth_20260930'
    @property
    def detail(self):return self.data/'detail_balance'
    @property
    def image(self):return self.data/'base.jpg'
    @property
    def landmarks(self):return self.data/'base.lms'

def character_assets(value=None):
    key=(value if value is not None else os.getenv('FEATHERTALK_CHARACTER','gentleman')).strip().lower()
    if key=='gentleman':
        return CharacterAssets(key,'gentleman',Path('/work/checkpoints/retrain_20260929/best.pth'),Path('/work/data'))
    if key=='qinche':
        raise RuntimeError('QinChe has an independent approved runtime. Run start_avatar.ps1 with SPECTER_AVATAR_CHARACTER=QinChe; do not load QinChe through the Gentleman engine.')
    raise ValueError('SPECTER_AVATAR_CHARACTER must be gentleman or QinChe')
