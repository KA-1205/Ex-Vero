import yaml
from typing import List
from .adapter import Adapter, TextAdapter

def load_adapters(config_path: str) -> List[Adapter]:
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    
    adapters = []
    for adapter_config in config.get('adapters', []):
        name = adapter_config.get('name')
        modality = adapter_config.get('modality')
        model = adapter_config.get('model')
        
        if modality == 'text':
            adapter = TextAdapter(name=name, model_name=model)
            adapters.append(adapter)
        else:
            # For step 1, we only have text. ImageAdapter will be implemented later.
            raise ValueError(f"Unsupported modality: {modality}")
    
    return adapters