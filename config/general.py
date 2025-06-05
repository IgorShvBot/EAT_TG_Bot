# import os
import yaml
from pathlib import Path

def load_general_settings(config_path=None):
    config_path = config_path or Path(__file__).parent / "settings.yaml"
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}