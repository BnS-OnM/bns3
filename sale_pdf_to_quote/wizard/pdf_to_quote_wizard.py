from odoo import models, fields
from odoo.exceptions import UserError

import base64
import io
import logging
import re

_logger = logging.getLogger(__name__)

# PDF library
try:
    import PyPDF2
except Exception:
    PyPDF2 = None
    _logger.warning("PyPDF2 not installed - PDF parsing will not work.")

# Fuzzy matcher
try:
    from rapidfuzz import fuzz
    FUZZY_MATCHER = "rapidfuzz"
except Exception:
    from difflib import SequenceMatcher
    FUZZY_MATCHER = "difflib"
    _logger.info("rapidfuzz not available, using difflib fallback")


# Studio property field where brand is stored (your provided field)
BRAND_PROP_FIELD = "product_properties.8b621cac637a8a24"

# Series keys that often appear in schema PDFs
SERIE_KEYS = ("vwl", "split", "pure", "tower", "vrc", "vr", "vih", "vp")


def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


class PdfToQuoteWizard(models.TransientModel):
    _name = "sale.pdf.to.quote.wizard"
    _description = "PDF to Quote Wizard"

    pdf_file = fields.Binary(string="PDF File", required=True)
    filename = fields.Char(string="Filename")

    partner_id = fields.Many2one(
        "res.partner", string="Customer", required=True,
        help="Select the customer for this quotation"
    )

    use_pdf_prices = fields.Boolean(
        string="Use PDF Prices", default=True,
        help="If enabled and prices are present in PDF, use them. Otherwise let Odoo determine prices."
    )

    name_prefix = fields.Char(
        string="Reference Prefix", default="GPT-001",
        help="Prefix for traceability in origin/client_order_ref"
    )

    # Optional brand restriction (UI field in wizard)
    restrict_to_brand = fields.Boolean(string="Restrict to brand", default=False)
    brand_filter = fields.Char(string="Brand name")

    # Dummy product (used when not matched on table PDFs)
    dummy_product_id = fields.Many2one(
        "product.product",
        string="Dummy product (niet gevonden)",
        help="Wordt gebruikt als placeholder wanneer een PDF-regel niet gematcht wordt."
    )

    # Result fields
    sale_order_id = fields.Many2one("sale.order", string="Created Quotation", readonly=True)
    matched_count = fields.Integer(string="Matched Lines", readonly=True)
    unmatched_count = fields.Integer(string="Unmatched Lines", readonly=True)
    unmatched_text = fields.Text(string="Unmatched Items Details", readonly=True)
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")

    PREFIX_BRAND_MAP = {
        "vaillant": "Vaillant",
    }

    # ---------------- Dummy product ----------------

    def _get_dummy_product(self):
        """
        Returns dummy product record.
        Priority:
        1) wizard dummy_product_id
        2) search by default_code = 'PDF-NOT-FOUND'
        """
        Product = self.env["product.product"].with_context(active_test=False)

        if self.dummy_product_id:
            return self.dummy_product_id

        dummy = Product.search([("default_code", "=", "PDF-NOT-FOUND")], limit=1)
        if dummy:
            return dummy

        raise UserError(
            "Geen dummy product ingesteld.\n"
            "Kies een dummy product in de wizard, of maak een product aan met default_code = 'PDF-NOT-FOUND'."
        )

    # ---------------- PDF extraction ----------------

    def _extract_text_from_pdf(self, pdf_bytes: bytes) -> str:
        if not PyPDF2:
            raise UserError("PyPDF2 library is not installed. Add PyPDF2==2.12.1 to requirements.txt")

        try:
            pdf_file_obj = io.BytesIO(pdf_bytes)
            pdf_reader = PyPDF2.PdfReader(pdf_file_obj)

            text_content = []
            for page in pdf_reader.pages:
                t = page.extract_text()
                if t:
                    text_content.append(t)

            full_text = "\n".join(text_content)
            if not full_text or len(full_text.strip()) < 50:
                raise UserError(
                    "PDF bevat geen leesbare tekst (waarschijnlijk scan). Gelieve een tekst-PDF te uploaden."
                )
            return full_text

        except UserError:
            raise
        except Exception as e:
            _logger.exception("Error extracting PDF text")
            raise UserError(f"Failed to extract text from PDF: {e}")

    # ---------------- Detect type ----------------

    def _detect_pdf_type(self, text: str) -> str:
        tl = text.lower()

        has_artikel_nr = "artikel nr" in tl or "artikelnr" in tl
        has_unit_price = "unit price" in tl or "eenheidsprijs" in tl

        table_pattern = re.compile(r"^\*?\d{5,}\s+\d+\s+", re.MULTILINE)
        has_table_pattern = len(table_pattern.findall(text)) > 2

        has_appliances = "appliances:" in tl
        has_controls = "controls:" in tl

        if has_artikel_nr or has_unit_price or has_table_pattern:
            _logger.info("Detected PDF type: table (quotation format)")
            return "table"
        if has_appliances or has_controls:
            _logger.info("Detected PDF type: bottom_bar (Appliances/Controls format)")
            return "bottom_bar"

        lines = text.split("\n")[:30]
        raise UserError(
            "Could not detect PDF format. First 30 lines:\n\n" + "\n".join(lines)
        )

    # ---------------- Parsers ----------------

    def _parse_eu_number(self, s):
        if not s:
            return None
        try:
            cleaned = s.strip().replace(".", "").replace(",", ".")
            return float(cleaned)
        except Exception:
            return None

    def _parse_bottom_bar(self, text: str):
        items = []

        appliances_match = re.search(r"Appliances:\s*(.+?)(?=Controls:|$)", text, re.DOTALL | re.IGNORECASE)
        controls_match = re.search(r"Controls:\s*(.+?)(?=$)", text, re.DOTALL | re.IGNORECASE)

        sections = []
        if appliances_match:
            sections.append(("Appliances", appliances_match.group(1)))
        if controls_match:
            sections.append(("Controls", controls_match.group(1)))

        for _, section_text in sections:
            parts = [p.strip() for p in section_text.split(",") if p.strip()]
            for p in parts:
                codes = self._extract_product_codes(p)
                items.append({
                    "codes": codes,
                    "desc": p,
                    "qty": 1,
                    "unit_price": None,
                })

        _logger.info("Parsed %s items from bottom bar format", len(items))
        return items

    def _extract_product_codes(self, text: str):
        codes = []
        patterns = [
            r"\bVR\d{2,4}\b",
            r"\bVRC\s*\d{2,4}\b",
            r"\bVWL\s*\d+(?:\.\d+)?\s*[A-Z]{1,4}\b",
            r"\bVIH\s*[A-Z]{1,4}\b",
            r"\bVP\sRW\s\d+/\d+\s*[A-Z]\b",
        ]
        for pattern in patterns:
            matches = re.findall(pattern, text, re.IGNORECASE)
            for m in matches:
                normalized = re.sub(r"\s+", "", m)
                codes.append(normalized)
        return codes

    def _parse_table_lines(self, text: str):
        items = []
        lines = text.split("\n")

        line_pattern = re.compile(
            r"^(\*?)(\d{5,})\s+(\d+)\s+(.+?)(?:\s+([\d.,]+)\s+([\d.,]+))?$"
        )

        for line in lines:
            line = line.strip()
            if not line:
                continue

            m = line_pattern.match(line)
            if not m:
                continue

            _, code, qty_str, desc, unit_price_str, _ = m.groups()

            try:
                qty = int(qty_str)
            except Exception:
                qty = 1

            unit_price = self._parse_eu_number(unit_price_str) if unit_price_str else None

            items.append({
                "code": code.strip(),
                "qty": qty,
                "desc": desc.strip(),
                "unit_price": unit_price,
            })

        _logger.info("Parsed %s items from table format", len(items))
        return items

    # ---------------- Matching helpers ----------------

    def _fuzzy_score(self, a: str, b: str) -> float:
        a = (a or "").lower()
        b = (b or "").lower()
        if FUZZY_MATCHER == "rapidfuzz":
            return float(fuzz.ratio(a, b))
        return SequenceMatcher(None, a, b).ratio() * 100.0

    def _auto_detect_brand(self, text: str):
        tl = _norm_text(text)
        for k, v in self.PREFIX_BRAND_MAP.items():
            if re.search(r"\b" + re.escape(k.lower()) + r"\b", tl):
                return v
        return None

    def _extract_series_tokens(self, s: str):
        tl = _norm_text(s)
        found = set()
        for k in SERIE_KEYS:
            if re.search(rf"\b{re.escape(k)}\b", tl):
                found.add(k)
        return found

    def _detect_series_from_text(self, text: str):
        series = set()
        for line in (text or "").split("\n"):
            if "arotherm" in _norm_text(line):
                series |= self._extract_series_tokens(line)
        if series:
            return series
        series |= self._extract_series_tokens(text or "")
        return series

    def _tokenize(self, s: str):
        tl = _norm_text(s)
        toks = [t for t in re.findall(r"[a-z0-9./]+", tl) if len(t) > 1]
        return toks

    def _token_group(self, token: str):
        token = token.strip()
        return [
            ("name", "ilike", token),
            ("default_code", "ilike", token),
            ("product_tmpl_id.name", "ilike", token),
            ("product_tmpl_id.default_code", "ilike", token),
        ]

    def _combine_or_groups(self, groups):
        if not groups:
            return []
        atoms = []
        for g in groups:
            atoms.extend(g)
        if not atoms:
            return []
        domain = []
        for _ in range(len(atoms) - 1):
            domain.append("|")
        domain.extend(atoms)
        return domain

    def _brand_domain(self, brand_name: str, for_model="product.product"):
        brand_name = (brand_name or "").strip()
        if not brand_name:
            return []
        if for_model == "product.product":
            return [(f"product_tmpl_id.{BRAND_PROP_FIELD}", "ilike", brand_name)]
        return [(BRAND_PROP_FIELD, "ilike", brand_name)]

    def _match_by_code(self, code, brand_name=None):
        Product = self.env["product.product"].with_context(active_test=False)
        Template = self.env["product.template"].with_context(active_test=False)

        codes = code if isinstance(code, list) else [code]
        for c in [x for x in codes if x]:
            dom_p = [("default_code", "=", c)]
            if brand_name:
                dom_p = self._brand_domain(brand_name, "product.product") + dom_p
            p = Product.search(dom_p, limit=1)
            if p:
                return p, 100.0, "code_exact", p.display_name

            dom_t = [("default_code", "=", c)]
            if brand_name:
                dom_t = self._brand_domain(brand_name, "product.template") + dom_t
            t = Template.search(dom_t, limit=1)
            if t:
                p2 = Product.search([("product_tmpl_id", "=", t.id)], limit=1)
                if p2:
                    return p2, 100.0, "code_template", p2.display_name

        return None, 0.0, None, None

    def _match_product(self, code=None, desc=None, brand_name=None, schema_mode=False):
        Product = self.env["product.product"].with_context(active_test=False)

        if code:
            p, sc, method, cand = self._match_by_code(code, brand_name=brand_name)
            if p:
                return p, sc, method, cand

        if not desc or len(desc.strip()) < 3:
            return None, 0.0, "no_desc", None

        tokens = self._tokenize(desc)

        if schema_mode:
            text = _norm_text(desc)
            has_family = any(tok in tokens for tok in ("vwl", "vrc", "vr", "vih", "vp"))
            size_match = re.search(r"\b\d{1,3}(?:[./]\d{1,2})?(?:\.\d{1,2})?\b", text)
            if not (has_family or size_match):
                return None, 0.0, "schema_too_vague", None

            if "vwl" in tokens:
                tokens = [t for t in tokens if t != "vwl"]
                tokens.insert(0, "vwl")
            if size_match:
                size_tok = size_match.group(0)
                tokens.insert(0, size_tok.replace(",", "."))

        tokens = tokens[:6]
        if not tokens:
            return None, 0.0, "no_tokens", None

        groups = [self._token_group(t) for t in tokens[:3]]
        domain = self._combine_or_groups(groups)
        if brand_name:
            domain = self._brand_domain(brand_name, "product.product") + (domain or [])

        candidates = Product.search(domain or [], limit=200)

        best_score = 0.0
        best_product = None
        best_name = None
        for p in candidates:
            score = self._fuzzy_score(desc, p.display_name)
            if score > best_score:
                best_score = score
                best_product = p
                best_name = p.display_name

        if best_product and best_score >= 80.0:
            return best_product, best_score, "fuzzy", best_name

        if best_product:
            return None, best_score, "fuzzy_failed", best_name

        return None, 0.0, "no_candidates", None

    # ---------------- Main action ----------------

    def action_create_quotation(self):
        self.ensure_one()

        if not self.pdf_file:
            raise UserError("Please upload a PDF file.")
        if not self.partner_id:
            raise UserError("Please select a customer.")

        # Keep original PDF bytes for attachment
        pdf_b64 = self.pdf_file
        pdf_bytes = base64.b64decode(self.pdf_file)

        # Extract text
        text = self._extract_text_from_pdf(pdf_bytes)

        # Detect type
        pdf_type = self._detect_pdf_type(text)

        # Parse
        if pdf_type == "table":
            parsed_items = self._parse_table_lines(text)
        else:
            parsed_items = self._parse_bottom_bar(text)

        if not parsed_items:
            raise UserError("No items found in PDF. Please check the file format.")

        _logger.info("Parsed %s items from PDF", len(parsed_items))

        # Brand (optional)
        brand_name = None
        if self.restrict_to_brand:
            brand_name = (self.brand_filter or "").strip() or self._auto_detect_brand(text)
            if brand_name:
                _logger.info("Brand restriction active on %s via %s", brand_name, BRAND_PROP_FIELD)
            else:
                _logger.info("Brand restriction enabled but no brand provided/detected; proceeding without brand filter")

        matched_lines = []
        unmatched_items = []
        products_dict = {}

        # --------- Schema (bottom_bar): add all products matching detected series across entire text ---------
        done_by_series = False
        if pdf_type == "bottom_bar":
            series = self._detect_series_from_text(text)
            _logger.info("Schema series detected in PDF text: %s", ", ".join(sorted(series)) if series else "—")

            if series:
                Product = self.env["product.product"].with_context(active_test=False)
                groups = [self._token_group(s) for s in series]
                domain = self._combine_or_groups(groups)
                if brand_name:
                    domain = self._brand_domain(brand_name, "product.product") + (domain or [])

                found = Product.search(domain or [], limit=5000)
                _logger.info("Schema series search found %s products", len(found))

                if found:
                    for p in found:
                        if p.id not in products_dict:
                            products_dict[p.id] = {
                                "qty": 1,
                                "price": None,
                                "desc": p.display_name or p.name,
                                "product": p,
                            }
                    matched_lines = parsed_items
                    done_by_series = True
                else:
                    unmatched_items.append({
                        "raw": f"Series: {', '.join(sorted(series))}",
                        "score": 0,
                        "candidate": "—",
                        "method": "series_search_empty",
                    })
            else:
                unmatched_items.append({
                    "raw": "No series tokens found in PDF text",
                    "score": 0,
                    "candidate": "—",
                    "method": "no_series",
                })

        # --------- fallback: per-item matching ---------
        if not done_by_series:
            for item in parsed_items:
                if pdf_type == "table":
                    code = item.get("code")
                    desc = item.get("desc", "")
                    qty = item.get("qty", 1)
                    unit_price = item.get("unit_price")
                else:
                    code = item.get("codes")
                    desc = item.get("desc", "")
                    qty = item.get("qty", 1)
                    unit_price = item.get("unit_price")

                product, score, method, candidate_name = self._match_product(
                    code=code,
                    desc=desc,
                    brand_name=brand_name,
                    schema_mode=(pdf_type == "bottom_bar"),
                )

                if product:
                    if product.id in products_dict:
                        products_dict[product.id]["qty"] += qty
                    else:
                        products_dict[product.id] = {
                            "qty": qty,
                            "price": unit_price if (unit_price and self.use_pdf_prices) else None,
                            "desc": desc if desc else product.display_name,
                            "product": product,
                        }
                    matched_lines.append(item)
                else:
                    unmatched_items.append({
                        "raw": f"Code: {code}, Desc: {desc}, Qty: {qty}",
                        "score": score,
                        "candidate": candidate_name or "No candidate found",
                        "method": method or "no_match",
                    })

                    # For table PDFs: add dummy product line so it appears in quotation
                    if pdf_type == "table":
                        dummy = self._get_dummy_product()
                        if dummy.id in products_dict:
                            products_dict[dummy.id]["qty"] += qty
                        else:
                            products_dict[dummy.id] = {
                                "qty": qty,
                                "price": unit_price if (unit_price and self.use_pdf_prices) else None,
                                "desc": (desc or "").strip() or (code or "").strip() or "Niet gevonden (PDF)",
                                "product": dummy,
                            }

        # Create sale order
        order_vals = {
            "partner_id": self.partner_id.id,
            "origin": f"{self.name_prefix} - {self.filename or 'PDF Import'}",
            "client_order_ref": f"{self.name_prefix} - {self.filename or 'PDF Import'}",
        }
        sale_order = self.env["sale.order"].create(order_vals)

        # NEW: attach uploaded PDF to the quotation
        attach_name = self.filename or "import.pdf"
        if not attach_name.lower().endswith(".pdf"):
            attach_name = f"{attach_name}.pdf"

        self.env["ir.attachment"].create({
            "name": attach_name,
            "type": "binary",
            "datas": pdf_b64,  # already base64 in Odoo binary field
            "mimetype": "application/pdf",
            "res_model": "sale.order",
            "res_id": sale_order.id,
        })

        # Create lines
        for _, line_data in products_dict.items():
            product = line_data["product"]
            line_vals = {
                "order_id": sale_order.id,
                "product_id": product.id,
                "product_uom_qty": line_data["qty"],
                "product_uom_id": product.uom_id.id,   # Odoo 19 field name
                "name": line_data["desc"],
            }
            if line_data.get("price") is not None:
                line_vals["price_unit"] = line_data["price"]

            self.env["sale.order.line"].create(line_vals)

        # Unmatched text
        unmatched_text = ""
        if unmatched_items:
            parts = []
            for it in unmatched_items:
                parts.append(
                    f"• {it['raw']}\n"
                    f"  Best candidate: {it.get('candidate','—')} (score: {it.get('score',0):.1f}%)\n"
                    f"  Method: {it.get('method','—')}\n"
                )
            unmatched_text = "\n".join(parts)

        self.write({
            "sale_order_id": sale_order.id,
            "matched_count": len(matched_lines),
            "unmatched_count": len(unmatched_items),
            "unmatched_text": unmatched_text,
            "state": "done",
        })

        _logger.info(
            "Created sale order %s: %s matched, %s unmatched (brand=%s)",
            sale_order.name, len(matched_lines), len(unmatched_items), brand_name
        )

        return {
            "type": "ir.actions.act_window",
            "name": "Created Quotation",
            "res_model": "sale.order",
            "res_id": sale_order.id,
            "view_mode": "form",
            "target": "current",
        }
