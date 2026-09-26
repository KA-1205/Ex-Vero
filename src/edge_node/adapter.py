from abc import ABC, abstractmethod
from typing import List, Tuple
import numpy as np
from fastembed import TextEmbedding

class Adapter(ABC):
    @abstractmethod
    def embed(self, payload: str) -> List[float]:
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def dim(self) -> int:
        pass

    @property
    @abstractmethod
    def modality(self) -> str:
        pass

class TextAdapter(Adapter):
    def __init__(self, name: str, model_name: str):
        self._name = name
        self._model_name = model_name
        self._model = TextEmbedding(model_name=model_name)
        # Get the dimension by embedding a dummy sentence
        dummy_embedding = list(self._model.embed(["dummy"]))[0]
        self._dim = len(dummy_embedding)

    def embed(self, payload: str) -> List[float]:
        # fastembed returns a generator of numpy arrays
        embedding = list(self._model.embed([payload]))[0]
        return embedding.tolist()

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def modality(self) -> str:
        return "text"