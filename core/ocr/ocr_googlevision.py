from core.ocr.base_ocr import BaseOCREngine
from google.cloud import vision
import os
# from PIL import Image
os.environ["GRPC_VERBOSITY"] = "NONE"
os.environ["GRPC_LOG_SEVERITY_LEVEL"] = "ERROR"

import os
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/bernhardlangwallner/Documents/05 Coding/direct-airfoil-474508-b2-42fb61af5650.json"

class GoogleOCR(BaseOCREngine):
    def __init__(self, api_key: str = None):
        self.client = vision.ImageAnnotatorClient()

    def extract_text(self, file_path: str) -> str:
        with open(file_path, "rb") as image_file:
            content = image_file.read()

        image = vision.Image(content=content)
        text_response = self.client.text_detection(image=image)
        return text_response.full_text_annotation.text