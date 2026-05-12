import os
from landingai_ade import LandingAIADE
from landingai_ade.lib import pydantic_to_json_schema
from pathlib import Path
import json
from PIL import Image, ImageDraw, ImageFont
from models.invoice_model import Invoice


class AgenticDocInvoiceProcessor:
    def __init__(self, model_id="dpt-2-latest"):
        self.client = LandingAIADE(
            apikey = os.environ["VISION_AGENT_API_KEY"],
            environment = "eu"  # or "production" or as needed
        )
        self.schema = pydantic_to_json_schema(Invoice)
        self.model_id = model_id

        
    def extract(self, prompt: str, file_path: str) -> str:
        parse_res = self.client.parse(
                                        document_url = file_path,
                                        model = self.model_id
        )

        md_file_path = "parsing_response.md"
        with open(md_file_path, "w", encoding="utf-8") as f:
            f.write(parse_res.markdown)


        extract_res = self.client.extract(
            schema = self.schema,
            markdown = Path(md_file_path)
        )

        image_paths_by_page = {1: file_path}  # your rendered input pages

        parse_dict = parse_res.model_dump()
        extract_dict = extract_res.model_dump()

        # dump json files with indent 4
        with open("extraction_dict.json", "w", encoding="utf-8") as f:
            json.dump(extract_dict, f, indent=4)

        with open("parse_dict.json", "w", encoding="utf-8") as f:
            json.dump(parse_dict, f, indent=4)

        self.visualize_extraction(image_paths_by_page, parse_dict, extract_dict)
        return extract_dict, parse_res.markdown


    def visualize_extraction(
        self,
        image_paths_by_page: dict[int, str],
        parse_result: dict,
        extract_result: dict,
        output_dir: str = "viz_pages"
    ):
        """Draws all extracted fields on their respective PDF/image page."""
        os.makedirs(output_dir, exist_ok=True)

        # Build chunk_id → groundings lookup from parse output
        chunk_to_groundings = parse_result.get("grounding", [])

    # Collect annotations: page → list of field + box + value
        page_annotations = {}

        for field, value in extract_result.get("extraction", {}).items():
            print(extract_result.get("extraction_metadata", {}))
            print(field)
            cid = extract_result.get("extraction_metadata", {}).get(field, {}).get("references")

            if len(cid) > 0:
                cid = cid[0]
            else:
                continue
            if not cid or cid not in chunk_to_groundings:
                continue

    #for grounding in chunk_to_groundings[cid]:
            grounding = chunk_to_groundings[cid]
            page = grounding["page"] + 1  # Assuming zero-based indexing from parse
            box = grounding["box"]
            annotation = {
                    "field": field,
                    "value": value,
                    "box": box
                    }

            page_annotations.setdefault(page, []).append(annotation)

        for page, img_path in image_paths_by_page.items():
            img = Image.open(img_path).convert("RGB")
            
            w, h = img.size

            draw = ImageDraw.Draw(img)

                # Try to use a readable font
            try:
                font = ImageFont.truetype("arial.ttf", 16)
            except:
                font = ImageFont.load_default()

            for ann in page_annotations.get(page, []):
                field = ann["field"]
                value = ann["value"]
                box = ann["box"]
                left, top, right, bottom = w*box["left"], h*box["top"], w*box["right"], h*box["bottom"]

                # Draw red rectangle
                draw.rectangle([left, top, right, bottom], outline="green", width=3)

                # Draw label
                label = f"{field}: {value}"
                try:
                    bbox = font.getbbox(label)  # works for Pillow >=8
                    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                except AttributeError:
                    text_w, text_h = font.getsize(label)  # fallback for older versions

                label_x = left
                label_y = top - text_h - 2
                if label_y < 0:
                    label_y = top + 2

                # Yellow background for label
                draw.rectangle([label_x, label_y, label_x + text_w, label_y + text_h], fill="green")
                draw.text((label_x, label_y), label, fill="black", font=font)

                    # Save final image
            output_path = os.path.join(output_dir, f"page_{page}_annotated.png")
            img.save(output_path)
            print(f"✅ Annotated page saved: {output_path}")