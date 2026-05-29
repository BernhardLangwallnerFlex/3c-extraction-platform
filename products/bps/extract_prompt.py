"""BPS (Belegprüfung Sach) extraction prompt (German).

Mirrors the vetcostcheck prompt structure but targets the BPS schema:
Handwerker-Belege (Rechnungen/Angebote) for property/contents insurance claims.
"""
from __future__ import annotations


def build_extract_prompt(
    *,
    ocr_text: str = "",
    subdocument_context: list[dict] | None = None,
    expected_items: int | None = None,
) -> str:
    """Build the extraction prompt for a single BPS sub-document.

    `subdocument_context` is accepted for signature compatibility with the
    pipeline but is unused for BPS (no per-subdocument context is produced).
    """
    if expected_items and expected_items > 0:
        items_hint = (
            f"WICHTIG: Dieser Beleg enthält voraussichtlich etwa {expected_items} Positionen. "
            f"Wenn du weniger als {expected_items} Positionen findest, überprüfe nochmals den "
            f"OCR-Text und das Bild — wahrscheinlich hast du Zeilen übersehen."
        )
    else:
        items_hint = ""

    return (
"Du bist ein Experte für die Prüfung von Handwerker-Belegen (Rechnungen und Angeboten) "
"im Bereich der Sachversicherung (Hausrat / Wohngebäude).\n"
"Deine Aufgabe ist es, aus dem untenstehenden Beleg strukturierte Informationen zu extrahieren "
"und sie ausschließlich als gültiges JSON-Objekt im definierten Schema zurückzugeben.\n"
"Erfinde keine Werte. Wenn ein Feld nicht sicher ermittelt werden kann, gib null zurück und "
"erkläre Unsicherheiten im Feld 'warnings'.\n"
f"{items_hint}\n"
"Der Beleg ist als Bild (visuelle Referenz) sowie als OCR-Text aus zwei unabhängigen OCR-Systemen "
"verfügbar. Der OCR-Text ist zwischen Doppel-Pipes (||) angegeben und enthält zwei mit "
"'OCR Source A' und 'OCR Source B' gekennzeichnete Abschnitte. Nutze beide OCR-Quellen, um Fehler "
"zu erkennen und zu korrigieren. Bei Widersprüchen zwischen den OCR-Quellen überprüfe mit dem Bild.\n\n"
"OCR-Text:\n"
f"||\n{ocr_text}\n||\n\n"
"Regeln für die Extraktion:\n"
"1. Keine Halluzinationen: Nur Werte extrahieren, die im OCR- oder Bildinhalt sichtbar oder eindeutig ableitbar sind.\n"
"2. Wenn ein Feld fehlt oder nicht eindeutig ist → null.\n"
"3. Strings: ohne führende/trailing Leerzeichen.\n"
"4. Geldbeträge: nur Ziffern und Punkt als Dezimaltrennzeichen, z. B. 1985.00. Tausenderpunkte entfernen.\n"
"5. Datumsformat: YYYY-MM-DD.\n"
"6. Währung: ISO-4217-Code (z. B. \"EUR\").\n"
"7. Belegart ('type'): 'invoice' für eine Rechnung, 'quote' für ein Angebot/Kostenvoranschlag. Wenn unklar → null.\n"
"8. 'sender' ist der Belegersteller bzw. Handwerker, der den Beleg ausgestellt hat (Firmenname, Anschrift, USt-IdNr in 'vatId'). Die IBAN/BIC des Belegerstellers gehören in 'payment'.\n"
"9. 'serviceProvider' ist der Dienstleister. Häufig identisch mit dem Belegersteller — wenn kein separater Dienstleister erkennbar ist, übernimm die Werte des 'sender'.\n"
"10. 'recipient' ist die Rechnungsanschrift (an wen der Beleg adressiert ist).\n"
"11. 'policyholder' ist der Versicherungsnehmer. Dieser steht oft NICHT in der Rechnungsanschrift, sondern im Betreff/Schadenbezug (z. B. 'Einbruchschaden <Name>, <Anschrift>') oder in einer beigefügten E-Mail. Wenn nicht ermittelbar → null-Felder.\n"
"12. 'damageLocation' ist der Schadenort. In ca. 95 % der Fälle entspricht er der Rechnungsanschrift ('recipient'). Wenn jedoch im Betreff oder in der E-Mail eine abweichende Schadenanschrift genannt wird, nutze diese und vermerke die Abweichung in 'warnings'.\n"
"13. Zeilen mit Summe, Zwischensumme, Nettosumme, MwSt, USt, Gesamt, Bruttosumme, Saldo → NICHT als items übernehmen; sie gehören in 'totals'.\n"
"14. Eine Position mit erkennbarer Beschreibung UND einem Preis → ein items-Eintrag. Erfasse pro Position: 'position' (Positionsnummer/laufende Nummer, falls vorhanden), 'name' (vollständiger Beschreibungstext, auch über mehrere Zeilen), 'qty' (Menge), 'unit', 'unitCode', 'unitPriceNet' (Einzelpreis/E-Preis), 'lineTotalNet' (Gesamtpreis der Zeile/G-Preis), 'taxRate' und 'discount' falls je Position angegeben (sonst null).\n"
"15. Einheit ('unit' = Rohtext wie 'Stk', 'm²', 'Std'; 'unitCode' = passender Code aus der folgenden Liste). Wenn keine Einheit angegeben ist oder keine passt, setze unit=null und unitCode=0 (Stück).\n"
"    0=Stk, 1=mm, 2=mm², 3=mm³, 4=cm, 5=cm², 6=cm³, 7=m, 8=m², 9=m³, 10=Woche, 11=Monat, 12=kg, 13=Std, 14=Tag, 15=km, 16=%, 17=l, 18=lm, 19=pauschal, 20=kWh, 21=Paar, 22=t, 23=AW, 24=Satz, 25=Stange, 26=g, 27=StWo, 28=Sonstige, 29=Kilowatt Peak, 30=Grad.\n"
"16. Quellreferenzen: gib einen kurzen Textausschnitt der extrahierten Zeile in source.snippet an.\n"
"17. Totals: normalisiere alle Zahlenwerte. Steuersatz/Steuerbetrag NUR extrahieren, wenn explizit angegeben; sonst tax.rate=null, tax.amount=null. 'discount' in totals = Rabatt/Skonto auf Belegebene.\n"
"18. Validierung: wenn totals.net, totals.tax.amount und totals.gross vorhanden sind, prüfe totals.net + totals.tax.amount ≈ totals.gross (Toleranz ±0.02) und vermerke Abweichungen in 'warnings'.\n"
"19. IBAN (DE) = 22 Zeichen; BIC = 8 oder 11 Zeichen, upper-case.\n"
"20. Beigefügte E-Mails oder Anschreiben sind KEINE eigenen Positionen. Nutze sie nur als Kontext für Versicherungsnehmer und Schadenort.\n"
"21. Alle Positionen in der Reihenfolge des Belegs extrahieren. Achte besonders darauf, ALLE Zeilen innerhalb von Tabellen zu erfassen — nicht zusammenfassen, nicht deduplizieren.\n\n"
"JSON-Ziel-Schema:\n"
"{\n"
"\"type\": \"invoice|quote|null\",\n"
"\"currency\": \"EUR|null\",\n"
"\"number\": \"string|null\",\n"
"\"issuedAt\": \"YYYY-MM-DD|null\",\n"
"\"sender\": {\n"
"  \"companyName\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\",\n"
"  \"city\": \"string|null\", \"country\": \"string|null\", \"contactPhone\": \"string|null\",\n"
"  \"contactMail\": \"string|null\", \"vatId\": \"string|null\"\n"
"},\n"
"\"serviceProvider\": {\n"
"  \"companyName\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\",\n"
"  \"city\": \"string|null\", \"country\": \"string|null\", \"contactPhone\": \"string|null\",\n"
"  \"contactMail\": \"string|null\", \"vatId\": \"string|null\"\n"
"},\n"
"\"payment\": { \"iban\": \"string|null\", \"bic\": \"string|null\", \"bankName\": \"string|null\", \"dueDate\": \"YYYY-MM-DD|null\" },\n"
"\"recipient\": {\n"
"  \"companyName\": \"string|null\", \"contactFirstname\": \"string|null\", \"contactName\": \"string|null\",\n"
"  \"street\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\",\n"
"  \"country\": \"string|null\", \"contactPhone\": \"string|null\", \"contactMail\": \"string|null\"\n"
"},\n"
"\"policyholder\": { \"name\": \"string|null\", \"address\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\", \"country\": \"string|null\" },\n"
"\"damageLocation\": { \"address\": \"string|null\", \"postcode\": \"string|null\", \"city\": \"string|null\", \"country\": \"string|null\" },\n"
"\"items\": [\n"
"  {\n"
"    \"position\": \"string|null\",\n"
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
