import json

# build_prompt_from_config("configs/extraction_config.json", use_ocr=True, use_vision=True, ocr_text=ocr_text)

def build_prompt_from_config_old(config_path="configs/extraction_config.json", use_ocr=False, use_vision=False, ocr_text=""):
    with open(config_path, "r") as f:
        config = json.load(f)   

    header = config["prompt_template"]["header"]
    if use_vision:
        header = header + "\n\n" + config["prompt_template"]["image_part"]
    
    if use_ocr:
        ocr_text = config["prompt_template"]["ocr_text"].format(ocr_text=ocr_text)
        header = header + "\n\n" + ocr_text

    footer = config["prompt_template"]["footer"]
    fmt = config["prompt_template"]["field_format"]

    body = "\n".join(
        fmt.format(readable_name=key, description=field["description"])
        for key, field in config["extraction_fields"].items()
    )

    return f"{header}\n\n{body}\n\n{footer}"

def build_prompt_for_analyze_document(config_path="configs/extraction_config.json", markdown_text=""):
    with open(config_path, "r") as f:
        config = json.load(f)   
    
    return config["analysis_prompt"].format(markdown_text=markdown_text)

def build_prompt_from_config(config_path="configs/extraction_config.json", use_ocr=False, use_vision=False, ocr_text="", animal_information={}):
    with open(config_path, "r") as f:
        config = json.load(f)   

    header = config["prompt_template"]["header"]
    if use_vision:
        header = header + "\n\n" + config["prompt_template"]["image_part"]

    if use_ocr:
        ocr_text = config["prompt_template"]["ocr_text"].format(ocr_text=ocr_text)
        header = header + "\n\n" + ocr_text

    footer = config["prompt_template"]["footer"]
    fmt = config["prompt_template"]["field_format"]

    if animal_information:
        animals_section = config["prompt_template"]["animals_section"]
        animals_string = "\n ".join([f"{animal['name']} (Tierart: {animal['species']}, Rasse: {animal['breed']})" 
                                    if animal['breed'] != ""
                                    else f"{animal['name']} (Tierart: {animal['species']})" 
                                    for animal in animal_information])
        animals_section = animals_section.format(animals_string=animals_string)

    body = "\n".join(
        fmt.format(readable_name=key, description=field["description"])
        for key, field in config["extraction_fields"].items()
    )

    return f"{header}\n\n{animals_section}\n\n{body}\n\n{footer}"