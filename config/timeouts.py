import yaml
from pathlib import Path

def load_timeouts(config_path=None):
    config_path = config_path or Path(__file__).parent / "timeouts.yaml"
    with open(config_path, 'r', encoding='utf-8') as file:
        return yaml.safe_load(file).get("timeouts", {})
