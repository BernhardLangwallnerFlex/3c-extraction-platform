from openai import OpenAI
from core.utils import convert_file_to_images, extract_json_from_response
import base64
from core.prompt_building.prompt_building import build_prompt_from_config
import json
import re


class GPTInvoiceProcessor:
    def __init__(self, model="gpt-4", name="gpt_processor", vision_model=None, api_key=None, ocr_engine=None):
        self.client = OpenAI(api_key=api_key)   
        self.model = model
        self.name = name
        self.vision_model = vision_model


    def extract(self, img_file_path: str, use_ocr=True, use_vision=True, markdown_text="", prompt="", subdocument_context={}) -> str:
        if use_ocr and markdown_text == "":
            raise ValueError("Not enough markdown text information to extract data from document.")
        if use_vision and not self.vision_model:
            raise ValueError("No vision model configured")

        if prompt == "":
            prompt = build_prompt_from_config("configs/extraction_config.json", use_ocr=use_ocr, use_vision=use_vision, ocr_text=markdown_text, animal_information=subdocument_context)

        content_blocks = [{"type": "text", "text": prompt}]

        if use_vision:
            images = convert_file_to_images(img_file_path)      
            for img_path in images:
                with open(img_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64}",
                            "detail": "auto"
                        }
                    }
                )
        model = self.vision_model if use_vision else self.model
        response = self.client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": content_blocks}],
                temperature=0,
                seed=42
            )

        usage = response.usage
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        # total_tokens = usage.total_tokens

        # Model-specific pricing (USD per 1K tokens)
        PROMPT_RATE = 0.00015
        COMPLETION_RATE = 0.0006
        cost = (prompt_tokens / 1000 * PROMPT_RATE) + (completion_tokens / 1000 * COMPLETION_RATE)
        #print(f"Cost of this call: ${cost:.6f}")

        #result = response.choices[0].message.content
        json_result = extract_json_from_response(response.choices[0].message.content)
        return json_result
