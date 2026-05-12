"""Vet-specific analyze prompt + schema overrides.

The vet analyze stage adds an `invoice_animals` field that isn't generic — it
maps each detected sub-invoice to the list of animals (with species/breed/etc.)
appearing on it. Other products will define their own analyze override or rely
on the generic core-level analyze prompt.

Captured verbatim from configs/extraction_config.json:analysis_prompt at the
time of the multi-product refactor.
"""
from __future__ import annotations


_ANALYZE_PROMPT_TEMPLATE = (
    "Du bist ein Experte für die Analyse von Verschreibungen und Rechnungen im Bereich Tierarzt und Tier-Physiotherapie.\n"
    "\n"
    "Du bekommst ein Dokument sowohl als Bilder (ein Bild pro Seite) als auch im Markdown-Format mit Seitennummern (Beispiel '--- PAGE 1 --- ...'). Das Dokument kann eine oder mehrere Rechnungen, Quittungen oder Verschreibungen enthalten. Manche Dokumente enthalten mehrere Seiten, die zu einer Rechnung gehören, andere Dokumente enthalten nur eine Rechnung, deren Information auf mehrere Seiten aufgeteilt ist. Ggf. kann ein Dokument auch unbrauchbar sein.\n"
    "\n"
    "WICHTIGE REGELN für die Erkennung von Rechnungsgrenzen:\n"
    "- Unterschiedliche Rechnungsnummern oder Quittungsnummern bedeuten IMMER separate Rechnungen, auch wenn Absender und Empfänger identisch sind.\n"
    "- Unterschiedliche Rechnungsdaten vom selben Absender deuten auf separate Rechnungen hin.\n"
    "- Gleicher Absender + gleicher Empfänger bedeutet NICHT automatisch dieselbe Rechnung. Achte auf eindeutige Kennzeichen wie Rechnungsnummer, Quittungsnummer und Datum.\n"
    "- Eine einzelne gescannte Seite kann das Ende eines Dokuments und den Anfang eines anderen enthalten. Weise die Seite der Rechnung zu, die den größten inhaltlichen Anteil auf der Seite hat. Wenn beide Rechnungen wesentliche Inhalte auf der Seite haben, weise die Seite beiden Rechnungen zu.\n"
    "- Seiten, die nur Datenschutzhinweise, TSE-Daten oder Zahlungsterminal-Belege enthalten, gehören zur vorhergehenden Rechnung und sind KEINE eigenständigen Rechnungen.\n"
    "- Nutze die Bilder, um visuelle Dokumentgrenzen zu erkennen: unterschiedliche Briefköpfe, Logos, Layouts oder Trennlinien deuten auf separate Dokumente hin.\n"
    "\n"
    "Deine Aufgabe ist es, das Dokument zu analysieren und folgende Fragen zu beantworten. Die Antworten sollen konsolidiert und im JSON-Format zurückgegeben werden.\n"
    " Frage 1: Welche Seiten enthalten nützliche Informationen zu einer Verschreibung oder Rechnung? Output: 'pages_with_invoice_information': <list of page numbers>, z.B. [1,2,4,5]. Beachte: Seitenzahlen starten bei 1. \n"
    "Frage 2: Wie viele unabhängige Rechnungen enthält das Dokument? Ouput: 'number_of_invoices': <number>.\n"
    "Frage 3: Welche Seiten gehören zu welcher Rechnung? Output: 'invoice_pages': <invoice_number>: <list of page numbers>, ... \n"
    "Frage 4: Wie viele Leistungen (z.B. Behandlungen oder Medikamente, die abgerechnet werden) befinden sich auf jeder Rechnung (meist sind das Zeilen in einer Tabelle)? Output: 'invoice_number_of_items': <invoice_number>: <number of items on invoice>, ...\n"
    "Frage 5: Welche Tiere werden genannt und welcher Spezies (z.B. Hund, Katze, etc.) bzw. Rasse (z.B. Labrador, Bulldog, etc.) gehören sie an? Dazu noch Informationen wie Geburtsdatum, Geschlecht, Chip-ID, Diagnose, etc. so weit vorhanden. Output als Liste von Dictionaries: 'animals': [{{'name': str, 'species': str, 'breed': str, 'birthDate': str, 'gender': str, 'chipId': str, 'diagnosis': str}}, {{'name': str,...}},...] und so weiter, falls es mehrere Tiere gibt.\n"
    "Frage 6: Welche Tiere gehören zu welcher Rechnung? Output: 'invoice_animals': {{<invoice_number>: [<liste der Tiere als Dictionaries wie in Frage 5>], ...}}. Wenn eine Rechnung kein Tier enthält, soll die Liste leer sein. Weise Tiere NUR den Rechnungen zu, auf denen sie tatsächlich erwähnt werden.\n"
    "\n Hier ist das Dokument im Markdown-Format: {markdown_text} "
)


def build_analyze_prompt(*, markdown_text: str = "") -> str:
    """Build the vet analyze prompt. Adds invoice_animals to the per-subdocument output.

    Body migrated verbatim from prompt_building.build_prompt_for_analyze_document,
    which read the same string from configs/extraction_config.json:analysis_prompt.
    """
    return _ANALYZE_PROMPT_TEMPLATE.format(markdown_text=markdown_text)


ANALYZE_OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Vetcostcheck analyze output",
    "description": "Vet-specific splitting/analysis output. The invoice_animals field is the vet-only override; everything else is generic and could be moved to core later.",
    "type": "object",
    "properties": {
        "pages_with_invoice_information": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "number_of_invoices": {"type": "integer", "minimum": 0},
        "invoice_pages": {
            "type": "object",
            "description": "Map of <invoice_number> (str) -> list of page numbers",
            "additionalProperties": {"type": "array", "items": {"type": "integer"}},
        },
        "invoice_number_of_items": {
            "type": "object",
            "description": "Map of <invoice_number> (str) -> count of items on that invoice",
            "additionalProperties": {"type": "integer"},
        },
        "animals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": ["string", "null"]},
                    "species": {"type": ["string", "null"]},
                    "breed": {"type": ["string", "null"]},
                    "birthDate": {"type": ["string", "null"]},
                    "gender": {"type": ["string", "null"]},
                    "chipId": {"type": ["string", "null"]},
                    "diagnosis": {"type": ["string", "null"]},
                },
            },
        },
        "invoice_animals": {
            "type": "object",
            "description": "Vet-specific: map of <invoice_number> (str) -> list of animal dicts (same shape as `animals`).",
            "additionalProperties": {
                "type": "array",
                "items": {"type": "object"},
            },
        },
    },
}
