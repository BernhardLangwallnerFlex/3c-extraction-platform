# BPS — Open Questions for the Product Owner / Domain Experts

These surfaced during the first-pass BPS extraction (v1) on the 7 sample PDFs in
`bps_sanierer_input/BPS_Input/`. v1 was deployed for expert testing; the answers
below should drive prompt/schema refinements.

Status: **awaiting product-owner input.** Date raised: 2026-05-29.

---

## 1. Price-less work slips (Arbeitsscheine / Leistungsnachweise)
**Sample:** `BPS_7.pdf` (doc 2 = Beleg-Nr 260288, doc 3 = 260240)

The PDF bundled one priced invoice plus two price-less work-time records (own
document numbers, no prices/totals). v1 split them out as separate Belege with
`type=null` and null prices.

**Question:** Should such price-less work slips be (a) kept as separate Belege,
(b) merged into the related invoice, or (c) ignored entirely?

## 2. Policyholder (Versicherungsnehmer) when the recipient is a WEG / Hausverwaltung
**Samples:** `BPS_5.pdf` (Bremische Hausverwaltung / "Eigentümergemeinschaft"), `BPS_7.pdf` (WEG Akazienstr. 35 c/o Hausverwaltung)

The invoice is addressed to a property manager / owners' association, not a named
person. v1 either left `policyholder` null or used the building entity.

**Question:** Who should `policyholder` be in these cases — the WEG/Eigentümer­gemeinschaft, the Hausverwaltung, or left null because it comes from the Auftrag?

## 3. Inferring the policyholder when not explicitly named
**Samples:** `BPS_1.pdf`, `BPS_4.pdf` ("(Mieter Martin Thees)"), `BPS_6.pdf`

When no explicit Versicherungsnehmer appears, v1 infers a likely person from the
invoice address or object context and records a `warning`.

**Question:** Is "best-effort inference with a warning" the desired behavior, or
should `policyholder` be left null whenever it isn't explicitly stated (since it
comes from the Auftrag anyway)?

## 4. Reverse-charge invoices (§13b, net = gross, tax = 0)
**Sample:** `BPS_1.pdf` (Telekom Deutschland GmbH, net = gross = 12,882.56, tax 0)

**Question:** Confirm this is a valid BPS case and that `tax = 0` / `net = gross`
for §13b Steuerschuldnerschaft des Leistungsempfängers is the correct
representation (vs. null tax).

## 5. Belegart values beyond invoice / quote
**Sample:** `BPS_7.pdf` doc2/doc3 (looked like Arbeitsschein/Leistungsnachweis)

v1's `type` enum is only `invoice` (Rechnung) / `quote` (Angebot). Other document
kinds appear in practice.

**Question:** Which additional Belegarten should be supported (e.g.
Lieferschein, Gutschrift, Kostenvoranschlag, Arbeitsschein/Leistungsnachweis)?

## 6. Customer number (Kundennummer) with no target field
**Sample:** `BPS_3.pdf` (Kundennummer 31427)

**Question:** Is the Kundennummer needed in the output, or is it redundant with
the Auftrag?

## 7. Steuernummer vs. USt-IdNr
**Sample:** `BPS_6.pdf` (Steuernummer present, no USt-IdNr → `vatId` left null)

**Question:** Should a plain Steuernummer (German tax number, not a USt-IdNr) be
captured in a separate field, or is `vatId` (USt-IdNr only) sufficient?

## 8. Schadennummer / claim reference (already deferred)
Per the design spec (§7), the claim number (e.g. `S12754-…` from the cover email
in `BPS_2.pdf`) is **not** extracted — it comes from the Auftrag. Re-confirm with
experts whether they want the Beleg's claim reference captured as a linking key.
