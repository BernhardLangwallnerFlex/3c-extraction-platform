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



def get_full_prompt(ocr_text="", animal_information={}, expected_items=None):
    if animal_information:
        animals_section = "\n".join([f"{animal['name']} (Tierart: {animal['species']}, Rasse: {animal['breed']})"
                                    if animal['breed'] != ""
                                    else f"{animal['name']} (Tierart: {animal['species']})"
                                    for animal in animal_information])
        animals_section = f"Die folgenden Tiere werden in der Rechnung oder Quittung erwähnt: {animals_section}. Diese Information ist wichtig für die Extrahierung der Leistungen auf der Rechnung oder Quittung."
    else:
        animals_section = ""

    if expected_items and expected_items > 0:
        items_hint = f"WICHTIG: Diese Rechnung enthält voraussichtlich etwa {expected_items} Leistungen/Positionen. Wenn du weniger als {expected_items} Positionen findest, überprüfe nochmals den OCR-Text und das Bild — wahrscheinlich hast du Zeilen übersehen."
    else:
        items_hint = ""
        
    return f"""
            Du bist ein Experte für die Analyse von Tierarzt- und Tierphysiotherapie-Rechnungen.
            Deine Aufgabe ist es, aus der untenstehenden Rechnung strukturierte Informationen zu extrahieren
            und sie ausschließlich als gültiges JSON-Objekt im definierten Schema zurückzugeben.
            Erfinde keine Werte. Wenn ein Feld nicht sicher ermittelt werden kann, gib null zurück und erkläre Unsicherheiten im Feld 'warnings'.
            {animals_section}
            {items_hint}
            Die Rechnung ist sowohl als Bild (visuelle Referenz) als auch als OCR-Text verfügbar.
            Der OCR-Text ist zwischen Doppel-Pipes (||) angegeben.
            OCR-Ergebnisse können fehlerhaft sein – überprüfe und korrigiere sie ggf. mit dem Bild.

            OCR-Text:
            ||
            {ocr_text}
            ||

            Regeln für die Extraktion:
            1. Keine Halluzinationen: Nur Werte extrahieren, die im OCR- oder Bildinhalt sichtbar oder eindeutig ableitbar sind.
            2. Wenn ein Feld fehlt oder nicht eindeutig ist → null.
            3. Strings: ohne führende/trailing Leerzeichen.
            4. Geldbeträge: nur Ziffern und Punkt, z. B. 36.31.
            5. Datumsformat: YYYY-MM-DD.
            6. Währung: ISO-4217-Code (z. B. "EUR").
            7. Zeilen mit Summe, Endsumme, USt, MwSt, Gesamt, Saldo, Offen, Netto, Brutto → nicht als items übernehmen.
            8. Eine Zeile mit klar erkennbarer Leistung oder Medikament → ein items-Eintrag.
            9. Mengen und Einheiten extrahieren, wenn eindeutig (z. B. 10 Tabletten, 1 Flasche).
            Wenn nicht vorhanden → qty=null, unit=null.
            10. GOT-Nummern: trenne in code, multiplier, raw; wenn keine gefunden → alle null.
            11. Quellreferenzen: gib OCR-IDs (z. B. 3-4, 3-5) in source.ids an und kurze Textausschnitte in source.snippet.
            12. Totals: normalisiere alle Zahlenwerte (Punkt als Dezimaltrennzeichen); Hauptsteuersatz verwenden.
            13. Validierung: totals.net + totals.tax.amount ≈ totals.gross (Toleranz ±0.02).
            14. IBAN (DE) = 22 Zeichen; BIC = 8 oder 11 Zeichen, upper-case.
            15. Die Leistungen ("items" unten) sollen in der Reihenfolge der Rechnung oder Quittung extrahiert werden. Meistens sind die Leistungen in Tabellenform angegeben. Achte daher besonders darauf ALLE Zeilen innerhalb von Tabellen genau zu analysieren und zu extrahieren. WICHTIG: Wenn eine Rechnung mehrere Behandlungstage enthält, wird dieselbe Leistung (z.B. "Folgeuntersuchung", "Kaltplasmabehandlung", "Materialpauschale") an jedem Tag separat aufgeführt. Jede Zeile ist ein eigener Eintrag — NICHT zusammenfassen oder deduplizieren.
            16. Die sogenannten GOT-Codes ("got" unten) sind ein- bis vierstellige Zahlen, oft in Klammern dargestellt. Falls in dem Format 'GOT 1234' angegeben, dann nur die Ziffern zurückgeben.

            JSON-Ziel-Schema:
            {{
            "type": "invoice|receipt|null",
            "currency": "EUR|null",
            "number": "string|null",
            "issuedAt": "YYYY-MM-DD|null",
            "serviceDates": ["YYYY-MM-DD", "..."],
            "sender": {{
                "practiceName": "string|null",
                "address": "string|null",
                "postcode": "string|null",
                "city": "string|null",
                "country": "string|null",
                "contactPhone": "string|null",
                "contactMail": "string|null",
                "vatId": "string|null"
            }},
            "clinicians": [
                {{ "name": "string|null", "title": "string|null" }}
            ],
            "payment": {{
                "iban": "string|null",
                "bic": "string|null",
                "bankName": "string|null",
                "dueDate": "YYYY-MM-DD|null"
            }},
            "recipient": {{
                "companyName": "string|null",
                "contactFirstname": "string|null",
                "contactName": "string|null",
                "street": "string|null",
                "postcode": "string|null",
                "city": "string|null",
                "country": "string|null",
                "contactPhone": "string|null",
                "contactMail": "string|null"
            }},
            "animals": [
                {{ "name": "string|null", "species": "string|null", "breed": "string|null" }}
            ],
            "items": [
                {{
                "name": "string|null",
                "got": {{ "code": "string|null", "multiplier": 1.0, "raw": "string|null" }},
                "animal": {{ "name": "string|null", "species": "string|null" }},
                "qty": 1.0,
                "unit": "string|null",
                "unitPriceNet": 0.00,
                "lineTotalNet": 0.00,
                "serviceDate": "YYYY-MM-DD|null",
                "source": {{ "ids": ["string"], "snippet": "string" }}
                }}
            ],
            "totals": {{
                "net": 0.00,
                "tax": {{ "rate": 19.0, "amount": 0.00 }},
                "gross": 0.00,
                "discount": 0.00
            }},
            "warnings": ["string"]
            }}

            Nur das vollständige JSON-Objekt ausgeben, ohne Erklärung oder Markdown.
            Wenn du unsicher bist, gib den wahrscheinlichsten Wert und eine kurze Begründung in warnings.
            """