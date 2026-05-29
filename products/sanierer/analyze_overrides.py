"""Sanierer-specific analyze/split prompt + schema.

Splits a (possibly multi-Beleg) PDF into sub-documents. Adapted from the BPS
analyze prompt: Schadensanierung terminology, no animal questions, with a rule
that email / cover pages are not independent Belege. Output JSON keys are kept
identical to vet/BPS (pages_with_invoice_information, number_of_invoices,
invoice_pages, invoice_number_of_items) because core/pipeline.py consumes those
exact keys.
"""
from __future__ import annotations


_ANALYZE_PROMPT_TEMPLATE = (
    "Du bist ein Experte für die Analyse von Schadensanierungs-Belegen (Rechnungen und Angeboten) "
    "im Bereich der Sachversicherung.\n"
    "\n"
    "Du bekommst ein Dokument sowohl als Bilder (ein Bild pro Seite) als auch im Markdown-Format "
    "mit Seitennummern (Beispiel '--- PAGE 1 --- ...'). Das Dokument kann einen oder mehrere Belege "
    "(Rechnungen oder Angebote) enthalten. Ein Beleg erstreckt sich häufig über mehrere Seiten; manche "
    "Dokumente enthalten zusätzlich eine weiterleitende E-Mail oder ein Anschreiben. Ggf. kann eine "
    "Seite auch unbrauchbar sein.\n"
    "\n"
    "WICHTIGE REGELN für die Erkennung von Beleggrenzen:\n"
    "- Unterschiedliche Belegnummern (Angebots-/Rechnungsnummern) bedeuten IMMER separate Belege, "
    "auch wenn Absender und Empfänger identisch sind.\n"
    "- Unterschiedliche Belegdaten vom selben Absender deuten auf separate Belege hin.\n"
    "- Mehrseitige Belege mit fortlaufenden Positionsnummern und 'Übertrag'-Zeilen gehören zu EINEM "
    "Beleg; eine neue Seite beginnt nicht automatisch einen neuen Beleg.\n"
    "- Seiten, die nur eine weiterleitende E-Mail, ein Anschreiben, Datenschutzhinweise oder "
    "Zahlungsterminal-Belege enthalten, sind KEINE eigenständigen Belege und werden dem zugehörigen "
    "Beleg zugeordnet oder ignoriert.\n"
    "- Nutze die Bilder, um visuelle Dokumentgrenzen zu erkennen: unterschiedliche Briefköpfe, Logos, "
    "Layouts oder Trennlinien deuten auf separate Belege hin.\n"
    "\n"
    "Deine Aufgabe ist es, das Dokument zu analysieren und folgende Fragen zu beantworten. Die "
    "Antworten sollen konsolidiert und im JSON-Format zurückgegeben werden.\n"
    " Frage 1: Welche Seiten enthalten nützliche Informationen zu einem Beleg (Rechnung/Angebot)? "
    "Output: 'pages_with_invoice_information': <list of page numbers>, z.B. [1,2,4,5]. Beachte: "
    "Seitenzahlen starten bei 1.\n"
    "Frage 2: Wie viele unabhängige Belege enthält das Dokument? Output: 'number_of_invoices': <number>.\n"
    "Frage 3: Welche Seiten gehören zu welchem Beleg? Output: 'invoice_pages': {{<beleg_number>: "
    "<list of page numbers>, ...}}.\n"
    "Frage 4: Wie viele abrechenbare Positionen (Einzelpositionen mit Preis, ohne Titel-Überschriften) "
    "befinden sich auf jedem Beleg? Output: 'invoice_number_of_items': {{<beleg_number>: <number of "
    "positions>, ...}}.\n"
    "\n Hier ist das Dokument im Markdown-Format: {markdown_text} "
)


def build_analyze_prompt(*, markdown_text: str = "") -> str:
    """Build the Sanierer analyze/split prompt."""
    return _ANALYZE_PROMPT_TEMPLATE.format(markdown_text=markdown_text)


ANALYZE_OUTPUT_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Sanierer analyze output",
    "description": "Sanierer splitting/analysis output. Keys match core's expectations; no per-subdocument context is produced for Sanierer.",
    "type": "object",
    "properties": {
        "pages_with_invoice_information": {
            "type": "array",
            "items": {"type": "integer", "minimum": 1},
        },
        "number_of_invoices": {"type": "integer", "minimum": 0},
        "invoice_pages": {
            "type": "object",
            "description": "Map of <beleg_number> (str) -> list of page numbers",
            "additionalProperties": {"type": "array", "items": {"type": "integer"}},
        },
        "invoice_number_of_items": {
            "type": "object",
            "description": "Map of <beleg_number> (str) -> count of positions on that Beleg",
            "additionalProperties": {"type": "integer"},
        },
    },
}
