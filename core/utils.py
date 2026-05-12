import base64
from PIL import Image
import re
import json
import fitz  # PyMuPDF
import tempfile
import csv
from PIL import Image
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential

_retry_log = structlog.get_logger()


def log_retry(retry_state):
    """Tenacity before_sleep callback — logs each retry with structured context."""
    _retry_log.warning(
        "retrying",
        attempt=retry_state.attempt_number,
        wait_s=round(retry_state.next_action.sleep, 1),
        error=str(retry_state.outcome.exception()),
    )


def strip_ocr_element_ids(text: str) -> str:
    """Remove LandingAI element anchors and table cell IDs from OCR markdown.

    These are random UUIDs regenerated on every parse call, adding noise
    and wasting tokens without contributing useful content.
    """
    # Remove <a id='...'></a> anchor tags (with optional whitespace/newlines around them)
    text = re.sub(r"\s*<a id=['\"][^'\"]*['\"]></a>\s*", "\n", text)
    # Remove id="..." attributes from table elements (<td id="3-a"> -> <td>, <table id="0-1"> -> <table>)
    text = re.sub(r'(<(?:t[dhr]|table))\s+id="[^"]*"', r'\1', text)
    # Collapse multiple blank lines into one
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def ensure_json_serializable(obj):
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        # Fix common issues
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        raise

# Extract the JSON content from the response
def extract_json_from_response(text):
    # Try 1: direct parse (already clean JSON)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try 2: extract from markdown code fence
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try 3: find the first { ... } or [ ... ] block via bracket matching
    for opener, closer in [('{', '}'), ('[', ']')]:
        start = text.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == '\\' and in_string:
                escape = True
                continue
            if c == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
        break

    raise ValueError(
        f"Could not extract valid JSON from model response. First 200 chars: {text[:200]}"
    )

def encode_image_to_base64(image_path):
    """
    Read an image file and encode it as a base64 string.
    
    Args:
        image_path: Path to the image file
        
    Returns:
        Base64 encoded string representation of the image
    """
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")

def encode_image(image_path):
    """Encode the image to base64."""
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except FileNotFoundError:
        print(f"Error: The file {image_path} was not found.")
        return None
    except Exception as e:  # Added general exception handling
        print(f"Error: {e}")
        return None

def encode_pdf(pdf_path):
    """Encode the pdf to base64."""
    try:
        with open(pdf_path, "rb") as pdf_file:
            return base64.b64encode(pdf_file.read()).decode('utf-8')
    except FileNotFoundError:
        print(f"Error: The file {pdf_path} was not found.")
        return None
    except Exception as e:  # Added general exception handling
        print(f"Error: {e}")
        return None

def resize_image_by_height(image_path, target_height=200):
    """
    Resize an image to a target height while maintaining aspect ratio.
    
    Args:
        image_path: Path to the image file (jpg, png, webp)
        target_height: Target height in pixels for the resized image
        
    Returns:
        PIL Image object resized to target height with aspect ratio preserved
    """
    # Open the image
    img = Image.open(image_path)
    
    # Get original dimensions
    original_width, original_height = img.size
    
    # Calculate aspect ratio
    aspect_ratio = original_width / original_height
    
    # Calculate new width based on target height
    target_width = int(target_height * aspect_ratio)
    
    # Resize the image
    resized_img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
    
    return resized_img

def convert_file_to_images(file_path: str) -> list:
    images = []

    if file_path.lower().endswith((".jpg", ".jpeg", ".png")):
        images.append(file_path)
    elif file_path.lower().endswith(".pdf"):
        with fitz.open(file_path) as doc:
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=150)
                temp_img_path = tempfile.mktemp(suffix=f"_{i}.png")
                pix.save(temp_img_path)
                images.append(temp_img_path)
    else:
        raise ValueError("Unsupported file format for direct vision input.")

    return images

def dict_of_dicts_to_csv(data: dict, filename: str):
    """
    Converts a dict of dicts like:
      {'algorithm1': {'a': 'value_1', 'b':'value_2'},
       'algorithm2': {'a': 'value_3','b':'value_4'}}
    into a CSV where outer keys are columns and inner keys are rows.
    """
    # Collect all unique inner keys (row labels)
    row_labels = sorted({k for v in data.values() for k in v.keys()})

    # Prepare header: first column = property name, then one column per algorithm
    header = ["property"] + list(data.keys())

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(header)

        # Write each property row
        for label in row_labels:
            row = [label] + [data[algo].get(label, "") for algo in data]
            writer.writerow(row)

    print(f"✅ CSV written to {filename}")