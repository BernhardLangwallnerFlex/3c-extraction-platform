"""Sanierer (Schadensanierung) extraction prompt (German).

Items-focused: extracts Belegpositionen (with an extra LV-Position) plus totals;
header/party data is intentionally not extracted (it comes from the Auftrag).
Mirrors the BPS prompt structure.
"""
from __future__ import annotations


def build_extract_prompt(
    *,
    ocr_text: str = "",
    subdocument_context: list[dict] | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single Sanierer sub-document.

    `subdocument_context` is accepted for signature compatibility with the
    pipeline but is unused for Sanierer (no per-subdocument context is produced).
    """
    if expected_items and expected_items > 0:
        items_hint = (
            f"WICHTIG: Dieser Beleg enthält voraussichtlich etwa {expected_items} abrechenbare Positionen. "
            f"Wenn du weniger als {expected_items} Positionen findest, überprüfe nochmals den OCR-Text und "
            f"das Bild — wahrscheinlich hast du Zeilen übersehen."
        )
    else:
        items_hint = ""

    return (
"Du bist ein Experte für die Prüfung von Schadensanierungs-Belegen (Rechnungen und Angeboten) "
"im Bereich der Sachversicherung. Solche Belege sind nach einem Leistungsverzeichnis (LV) aufgebaut "
"und hierarchisch in Titel und Positionen gegliedert.\n"
"Deine Aufgabe ist es, aus dem untenstehenden Beleg die abrechenbaren Positionen und die Summen zu "
"extrahieren und sie ausschließlich als gültiges JSON-Objekt im definierten Schema zurückzugeben.\n"
"Erfinde keine Werte. Wenn ein Feld nicht sicher ermittelt werden kann, gib null zurück und erkläre "
"Unsicherheiten im Feld 'warnings'.\n"
f"{items_hint}\n"
"Der Beleg ist als Bild (visuelle Referenz) sowie als OCR-Text aus zwei unabhängigen OCR-Systemen "
"verfügbar. Der OCR-Text ist zwischen Doppel-Pipes (||) angegeben und enthält zwei mit 'OCR Source A' "
"und 'OCR Source B' gekennzeichnete Abschnitte. Nutze beide OCR-Quellen, um Fehler zu erkennen und zu "
"korrigieren. Bei Widersprüchen zwischen den OCR-Quellen überprüfe mit dem Bild.\n\n"
"OCR-Text:\n"
f"||\n{ocr_text}\n||\n\n"
"Regeln für die Extraktion:\n"
"1. Keine Halluzinationen: Nur Werte extrahieren, die im OCR- oder Bildinhalt sichtbar oder eindeutig ableitbar sind.\n"
"2. Wenn ein Feld fehlt oder nicht eindeutig ist → null.\n"
"3. Strings: ohne führende/trailing Leerzeichen.\n"
"4. Geldbeträge: nur Ziffern und Punkt als Dezimaltrennzeichen, z. B. 408.10. Tausenderpunkte entfernen. Negative Beträge (Rabatt/Gutschrift) als negative Zahl behalten.\n"
"5. Datumsformat: YYYY-MM-DD.\n"
"6. Währung: ISO-4217-Code (z. B. \"EUR\").\n"
"7. Belegart ('type'): 'invoice' für eine Rechnung, 'quote' für ein Angebot. Wenn unklar → null.\n"
"8. HIERARCHIE: Sanierer-Belege sind in Titel (z. B. '01. Allgemeines'), Unter-Titel (z. B. '01.01. Einrichtung Baustelle') und abrechenbare Einzelpositionen (z. B. '05.01.001.') gegliedert. NUR abrechenbare Einzelpositionen mit Menge und Preis sind items. Titel- und Unter-Titel-Überschriften (ohne Menge/Preis) sind KEINE items.\n"
"9. PFLICHTFELDER 'position' UND 'lvPosition' — fülle BEIDE für JEDE Position aus, wann immer sie im Beleg sichtbar sind. Lass sie NIEMALS leer, wenn eine Nummer vorhanden ist; das Auslassen dieser Nummern ist ein Fehler. Je Position gibt es ZWEI Nummern: 'position' = die laufende Positionsnummer aus der ersten Spalte (Spalte 'Pos.', z. B. '05.01.001.'); 'lvPosition' = die LV-Nummer (Leistungsverzeichnis-Referenz), die am ANFANG der ausführlichen Beschreibungszeile (zweite Zeile der Position) steht (z. B. '05.04.001') und sich in der Regel von 'position' unterscheidet. Übernimm beide Nummern als String exakt wie im Beleg. Nur falls ausnahmsweise eine der beiden Nummern wirklich nicht existiert, setze sie auf null.\n"
"10. 'name': der vollständige Beschreibungstext der Position (ggf. über mehrere Zeilen).\n"
"11. Einheit: 'unit' = Rohtext der Spalte ME (z. B. 'M2', 'ST', 'H', '%'); 'unitCode' = passender Code aus der folgenden Liste. Wenn keine Einheit angegeben ist oder keine passt, setze unit=null und unitCode=0 (Stück).\n"
"    0=Stk, 1=mm, 2=mm², 3=mm³, 4=cm, 5=cm², 6=cm³, 7=m, 8=m², 9=m³, 10=Woche, 11=Monat, 12=kg, 13=Std, 14=Tag, 15=km, 16=%, 17=l, 18=lm, 19=pauschal, 20=kWh, 21=Paar, 22=t, 23=AW, 24=Satz, 25=Stange, 26=g, 27=StWo, 28=Sonstige, 29=Kilowatt Peak, 30=Grad.\n"
"    Hinweis: Spaltenwerte 'M2'→8 (m²), 'ST'→0 (Stück), 'H'→13 (Std), '%'→16, 'M'→7, 'lfm'/'lm'→18.\n"
"12. Prozent-/Pauschal-/Rabattpositionen (z. B. Aufwandspauschale, Regiekosten, AXA-Rabatt): unit='%', unitCode=16, qty=der angegebene Bruchwert (z. B. 0.010 für '0,010 %'), und 'lineTotalNet' wie ausgewiesen (negativ bei Rabatt). Diese Zeilen sind items.\n"
"13. 'unitPriceNet' = Einzelpreis, 'lineTotalNet' = Gesamtpreis der Zeile.\n"
"14. taxRate/discount je Position nur, wenn explizit je Zeile angegeben; sonst null.\n"
"15. KEINE items: 'Übertrag', 'Zusammenstellung Titel', 'Summe ...', 'SE Basis', 'Nettogesamtpreis', 'Umsatzsteuer', 'Gesamtsumme', Titel-/Unter-Titel-Überschriften.\n"
"16. Totals: 'Nettogesamtpreis' → totals.net; 'Umsatzsteuer' (Satz und Betrag) → totals.tax.rate / totals.tax.amount; 'Gesamtsumme' → totals.gross. Ein Rabatt auf Belegebene → totals.discount.\n"
"17. Validierung: Die Summe aller items.lineTotalNet sollte ≈ totals.net sein (Toleranz ±0.02, Rabattzeilen mindern die Summe). Außerdem totals.net + totals.tax.amount ≈ totals.gross. Vermerke Abweichungen in 'warnings'.\n"
"18. Quellreferenzen: gib einen kurzen Textausschnitt der extrahierten Zeile in source.snippet an.\n"
"19. Alle Positionen in der Reihenfolge des Belegs extrahieren — nicht zusammenfassen, nicht deduplizieren.\n\n"
"WICHTIGES BEISPIEL für die Positions-Extraktion (so MUSST du 'position' und 'lvPosition' trennen):\n"
"Beleg-Zeile:\n"
"  05.01.001.   Trocknung bis 10m² Grundfläche            1   ST   408,10 €   408,10 €\n"
"               05.04.001 Trocknung bis 10m² Grundfläche für Raum-, Wand-, ...\n"
"Korrekt extrahiert als item:\n"
"  { \"position\": \"05.01.001.\", \"lvPosition\": \"05.04.001\", \"name\": \"Trocknung bis 10m² Grundfläche für Raum-, Wand-, ...\", \"qty\": 1, \"unit\": \"ST\", \"unitCode\": 0, \"unitPriceNet\": 408.10, \"lineTotalNet\": 408.10 }\n"
"Hier ist '05.01.001.' die laufende Position (erste Spalte) und '05.04.001' die LV-Nummer (Anfang der Beschreibungs-Detailzeile). BEIDE müssen gesetzt sein.\n\n"
"JSON-Ziel-Schema:\n"
"{\n"
"\"type\": \"invoice|quote|null\",\n"
"\"currency\": \"EUR|null\",\n"
"\"number\": \"string|null\",\n"
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"items\": [\n"
"  {\n"
"    \"position\": \"string|null\",\n"
"    \"lvPosition\": \"string|null\",\n"
"    \"name\": \"string|null\",\n"
"    \"qty\": \"number|null\",\n"
"    \"unit\": \"string|null\",\n"
"    \"unitCode\": \"integer (0-30, default 0)\",\n"
"    \"unitPriceNet\": \"number|null\",\n"
"    \"lineTotalNet\": \"number|null\",\n"
"    \"taxRate\": \"number|null\",\n"
"    \"discount\": \"number|null\",\n"
"    \"source\": { \"snippet\": \"string\" }\n"
"  }\n"
"],\n"
"\"totals\": { \"net\": \"number|null\", \"tax\": { \"rate\": \"number|null\", \"amount\": \"number|null\" }, \"gross\": \"number|null\", \"discount\": \"number|null\" },\n"
"\"warnings\": [\"string\"]\n"
"}\n\n"
"Nur das vollständige JSON-Objekt ausgeben, ohne Erklärung oder Markdown.\n"
"Wenn du unsicher bist, gib den wahrscheinlichsten Wert und eine kurze Begründung in warnings."
    )
