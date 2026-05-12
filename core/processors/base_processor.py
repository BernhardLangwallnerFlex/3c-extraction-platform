from abc import ABC, abstractmethod

class BaseProcessor(ABC):
    @abstractmethod
    def extract(self, prompt: str, file_path: str) -> dict:
        pass