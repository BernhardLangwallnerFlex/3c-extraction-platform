from openai import AzureOpenAI
from core.utils import convert_file_to_images, extract_json_from_response, log_retry, sampling_params
import base64
import os
from core.prompt_building.prompt_building import build_prompt_from_config
import json
import re
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential
from core.llm_errors import call_with_vision_fallback, is_retryable

log = structlog.get_logger()


# See core/llm_errors.is_retryable — permanent client errors must not be retried.
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=30),
       retry=retry_if_exception(is_retryable), before_sleep=log_retry, reraise=True)
def _call_openai(client, model, content_blocks):
    """Call Azure OpenAI with retry on transient failures."""
    return client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content_blocks}],
        **sampling_params(model),
    )


class AzureInvoiceProcessor:
    def __init__(self, model="gpt-4", name="azure_processor", vision_model=None, api_key=None, azure_endpoint=None, api_version="2024-02-15-preview", ocr_engine=None):
        """
        Initialize Azure OpenAI client for invoice processing.
        
        Args:
            model: Default text model name (e.g., "gpt-4")
            name: Processor name identifier
            vision_model: Vision model name for image processing (e.g., "gpt-4o")
            api_key: Azure OpenAI API key
            azure_endpoint: Azure OpenAI endpoint URL (e.g., "https://your-resource.openai.azure.com/")
            api_version: Azure API version (default: "2024-02-15-preview")
            ocr_engine: Optional OCR engine (not used in current implementation)
        """
        if not api_key:
            raise ValueError("Azure API key is required")
        if not azure_endpoint:
            raise ValueError("Azure endpoint is required")

        # If caller passed None/"" (e.g., missing env var), fall back to default.
        if not api_version:
            api_version = "2024-02-15-preview"
            
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version
        )
        self.model = model
        self.name = name
        self.vision_model = vision_model
        self.api_version = api_version


    def extract(self, img_file_path: str, use_ocr=True, use_vision=True, markdown_text="", prompt="", subdocument_context={}) -> str:
        """
        Extract invoice data from an image file using Azure OpenAI.

        Args:
            img_file_path: Path to the image file (JPG, PNG, or PDF)
            use_ocr: Whether to use OCR text in the prompt
            use_vision: Whether to include images in the vision API call
            markdown_text: OCR-extracted markdown text
            prompt: Custom prompt (if empty, will be built from config)
            subdocument_context: Optional per-subdocument context (product-specific)

        Returns:
            JSON string containing extracted invoice data
        """
        if use_ocr and markdown_text == "" and prompt == "":
            raise ValueError("Not enough markdown text information to extract data from document.")
        if use_vision and not self.vision_model:
            raise ValueError("No vision model configured")

        if prompt == "":
            prompt = build_prompt_from_config("configs/extraction_config.json", use_ocr=use_ocr, use_vision=use_vision, ocr_text=markdown_text, animal_information=subdocument_context)

        content_blocks = [{"type": "text", "text": prompt}]

        if use_vision:
            images = convert_file_to_images(img_file_path)
            for img_path in images:
                if os.getenv("DEBUG_EXTRACT"):
                    try:
                        from PIL import Image as _Img, ImageStat as _Stat
                        import os as _os
                        with _Img.open(img_path).convert("L") as _im:
                            _st = _Stat.Stat(_im)
                            log.info("debug_image_sent", path=img_path, w=_im.width, h=_im.height,
                                     bytes=_os.path.getsize(img_path),
                                     px_mean=round(_st.mean[0], 2), px_stddev=round(_st.stddev[0], 2))
                    except Exception as _e:  # noqa: BLE001
                        log.info("debug_image_err", err=str(_e))
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
        # The model is deliberately NOT switched on fallback: dropping to
        # self.model would change extraction behaviour on top of the
        # degradation, and the vision model handles a text-only request fine.
        response, vision_dropped = call_with_vision_fallback(
            _call_openai, self.client, model, content_blocks,
        )

        usage = response.usage
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens

        # gpt-5.4 GlobalStandard (<272k context), Germany West Central, USD per 1K tokens.
        # Azure Foundry list price (2026-06): input €2.16 / output €12.91 per 1M tokens,
        # converted at 1 USD = 0.8601 EUR -> ~$2.51 / $15.01 per 1M. Update if the model,
        # region, or rate changes. (Cheaper cached-input rate €0.22/1M is not tracked here.)
        PROMPT_RATE = 0.00251
        COMPLETION_RATE = 0.01501
        cost = (prompt_tokens / 1000 * PROMPT_RATE) + (completion_tokens / 1000 * COMPLETION_RATE)

        log.info(
            "llm_call",
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=round(cost, 6),
        )

        if os.getenv("DEBUG_EXTRACT"):
            _raw = response.choices[0].message.content or ""
            log.info(
                "debug_extract",
                use_vision=use_vision,
                num_images=(len(content_blocks) - 1),
                raw_len=len(_raw),
                has_position=('"position"' in _raw),
                has_lvposition=('"lvPosition"' in _raw),
                raw_head=_raw[:240],
            )

        json_result = extract_json_from_response(response.choices[0].message.content)
        if vision_dropped and isinstance(json_result, dict):
            # Rides on the result, not on self: subdocuments are extracted in
            # parallel threads sharing one processor instance. The pipeline
            # pops this key, so it never reaches the consumer.
            json_result["_vision_dropped"] = True
        return json_result
