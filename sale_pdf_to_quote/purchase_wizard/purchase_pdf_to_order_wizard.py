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


def _norm_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _norm_lower(s: str) -> str:
    return _norm_text(s).lower()


class PurchasePdfToOrderWizard(models.TransientModel):
    _name = "purchase.pdf.to.order.wizard"
    _description = "Purchase PDF to Order Wizard"

    pdf_file = fields.Binary(string="PDF File", required=True)
    filename = fields.Char(string="Filename")

    partner_id = fields.Many2one(
        "res.partner",
        string="Vendor",
        domain=[("supplier_rank", ">", 0)],
        help="Select the vendor for this purchase order. If not provided, wizard will attempt to detect from PDF."
    )

    use_pdf_prices = fields.Boolean(
        string="Use PDF Prices",
        default=True,
        help="If enabled and prices are present in PDF, use them. Otherwise let Odoo determine prices."
    )

    name_prefix = fields.Char(
        string="Reference Prefix",
        default="PDF-PO",
        help="Prefix for traceability in origin"
    )

    # Result
    purchase_order_id = fields.Many2one("purchase.order", string="Created RFQ/PO", readonly=True)
    matched_count = fields.Integer(string="Matched Lines", readonly=True)
    unmatched_count = fields.Integer(string="Unmatched Lines", readonly=True)
    unmatched_text = fields.Text(string="Unmatched Details", readonly=True)
    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")

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
                raise UserError("PDF bevat geen leesbare tekst (waarschijnlijk scan). Upload een tekst-PDF.")
            return full_text
        except UserError:
            raise
        except Exception as e:
            _logger.exception("Error extracting PDF text")
            raise UserError(f"Failed to extract text from PDF: {e}")

    # ---------------- Parsing helpers ----------------

    def _parse_eu_number(self, s):
        """EU: 1.234,56 -> 1234.56 | 123,45 -> 123.45"""
        if not s:
            return None
        try:
            cleaned = s.strip()
            cleaned = cleaned.replace(" ", "")
            cleaned = cleaned.replace(".", "").replace(",", ".")
            return float(cleaned)
        except Exception:
            return None

    def _parse_discount_percent(self, s):
        if not s:
            return None
        s = s.strip().replace(" ", "")
        m = re.search(r"(-?\d+(?:[.,]\d+)?)\s*%", s)
        if not m:
            return None
        val = self._parse_eu_number(m.group(1))
        return val

    def _detect_vendor_name(self, text: str):
        """
        Zeer simpele heuristiek. Als je PDF’s vaste labels hebben (Supplier/Vendor),
        kan je dit later verfijnen.
        """
        tl = _norm_lower(text)

        # typische labels
        patterns = [
            r"(?:vendor|supplier|leverancier)\s*[:\-]\s*(.+)",
            r"(?:from)\s*[:\-]\s*(.+)",
        ]
        for pat in patterns:
            m = re.search(pat, tl, re.IGNORECASE)
            if m:
                name = _norm_text(m.group(1))
                name = name.split("\n")[0].strip()
                name = re.split(r"\s{2,}", name)[0].strip()
                if len(name) >= 2:
                    return name

        first_lines = [l.strip() for l in text.split("\n")[:10] if l.strip()]
        if first_lines:
            cand = first_lines[0].strip()
            if 2 <= len(cand) <= 80 and not re.search(r"\d{3,}", cand):
                return cand

        return None

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

    def _tokenize(self, s: str):
        tl = _norm_lower(s)
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

    def _product_from_template(self, tmpl):
        if not tmpl:
            return None
        if hasattr(tmpl, "product_variant_id") and tmpl.product_variant_id:
            return tmpl.product_variant_id
        return self.env["product.product"].with_context(active_test=False).search(
            [("product_tmpl_id", "=", tmpl.id)], limit=1
        )

    def _match_by_vendor_fields(self, vendor, code, desc):
        """
        FIRST: search in seller_ids/product_code and seller_ids/product_name for that vendor.
        - seller_ids = product.supplierinfo on product.template
        """
        if not vendor:
            return None, 0.0, "no_vendor", None

        ProductTmpl = self.env["product.template"].with_context(active_test=False)
        SupplierInfo = self.env["product.supplierinfo"].with_context(active_test=False)

        code_n = (code or "").strip()
        desc_n = _norm_lower(desc or "")

        # 1) seller_ids.product_code
        if code_n:
            si = SupplierInfo.search(
                [("partner_id", "=", vendor.id), ("product_code", "ilike", code_n)],
                limit=1
            )
            if si:
                p = si.product_id or self._product_from_template(si.product_tmpl_id)
                if p:
                    return p, 100.0, "seller_ids.product_code", p.display_name

            tmpl = ProductTmpl.search(
                [("seller_ids.partner_id", "=", vendor.id), ("seller_ids.product_code", "ilike", code_n)],
                limit=1
            )
            if tmpl:
                p = self._product_from_template(tmpl)
                if p:
                    return p, 100.0, "seller_ids.product_code_tmpl", p.display_name

        # 2) seller_ids.product_name (fuzzy)
        if desc_n:
            tmpls = ProductTmpl.search(
                [("seller_ids.partner_id", "=", vendor.id), ("seller_ids.product_name", "ilike", desc_n[:25])],
                limit=80
            )
            best_p = None
            best_sc = 0.0
            best_name = None
            for tmpl in tmpls:
                for si in tmpl.seller_ids.filtered(lambda s: s.partner_id.id == vendor.id):
                    cand_name = si.product_name or tmpl.name
                    sc = self._fuzzy_score(desc, cand_name)
                    if sc > best_sc:
                        best_sc = sc
                        best_p = self._product_from_template(tmpl)
                        best_name = best_p.display_name if best_p else tmpl.display_name
            if best_p and best_sc >= 80.0:
                return best_p, best_sc, "seller_ids.product_name", best_name

        return None, 0.0, "no_seller_match", None

    def _match_by_code(self, code):
        Product = self.env["product.product"].with_context(active_test=False)
        Template = self.env["product.template"].with_context(active_test=False)

        codes = code if isinstance(code, list) else [code]
        for c in [x for x in codes if x]:
            dom_p = [("default_code", "=", c)]
            p = Product.search(dom_p, limit=1)
            if p:
                return p, 100.0, "code_exact", p.display_name

            dom_t = [("default_code", "=", c)]
            t = Template.search(dom_t, limit=1)
            if t:
                p2 = Product.search([("product_tmpl_id", "=", t.id)], limit=1)
                if p2:
                    return p2, 100.0, "code_template", p2.display_name

        return None, 0.0, None, None

    def _match_product(self, vendor=None, code=None, desc=None):
        """
        Matching order:
        1) seller_ids.product_code / seller_ids.product_name (vendor-specific)
        2) default_code exact (global)
        3) fuzzy on product fields (global)
        """
        Product = self.env["product.product"].with_context(active_test=False)

        # 1) FIRST vendor seller fields
        if vendor:
            p, sc, method, cand = self._match_by_vendor_fields(vendor, code, desc)
            if p:
                return p, sc, method, cand

        # 2) then try exact by default_code
        if code:
            p, sc, method, cand = self._match_by_code(code)
            if p:
                return p, sc, method, cand

        # 3) fuzzy / token matching on global product
        if not desc or len(desc.strip()) < 3:
            return None, 0.0, "no_desc", None

        tokens = self._tokenize(desc)[:6]
        if not tokens:
            return None, 0.0, "no_tokens", None

        groups = [self._token_group(t) for t in tokens[:3]]
        domain = self._combine_or_groups(groups)
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

    def action_create_purchase_order(self):
        """
        Main action to create a purchase order from the PDF.
        """
        self.ensure_one()

        if not self.pdf_file:
            raise UserError("Please upload a PDF file.")

        # Keep original PDF bytes for attachment
        pdf_b64 = self.pdf_file
        pdf_bytes = base64.b64decode(self.pdf_file)

        # Extract text
        text = self._extract_text_from_pdf(pdf_bytes)

        # Detect vendor
        vendor = self.partner_id
        if not vendor:
            vendor_name = self._detect_vendor_name(text)
            if vendor_name:
                Partner = self.env["res.partner"]
                vendor = Partner.search([("name", "ilike", vendor_name)], limit=1)
                if vendor:
                    _logger.info("Vendor detected and found: %s", vendor.name)
                else:
                    _logger.info("Vendor name detected (%s) but not found in database", vendor_name)

        if not vendor:
            raise UserError(
                "No vendor provided or detected.\n"
                "Please select a vendor in the wizard or ensure the PDF contains vendor information."
            )

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

        matched_lines = []
        unmatched_items = []
        products_dict = {}

        # Match products
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
                vendor=vendor,
                code=code,
                desc=desc,
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

        # Create purchase order
        order_vals = {
            "partner_id": vendor.id,
            "origin": f"{self.name_prefix} - {self.filename or 'PDF Import'}",
        }
        purchase_order = self.env["purchase.order"].create(order_vals)

        # Attach uploaded PDF to the purchase order
        attach_name = self.filename or "import.pdf"
        if not attach_name.lower().endswith(".pdf"):
            attach_name = f"{attach_name}.pdf"

        self.env["ir.attachment"].create({
            "name": attach_name,
            "type": "binary",
            "datas": pdf_b64,
            "mimetype": "application/pdf",
            "res_model": "purchase.order",
            "res_id": purchase_order.id,
        })

        # Create lines
        for _, line_data in products_dict.items():
            product = line_data["product"]
            line_vals = {
                "order_id": purchase_order.id,
                "product_id": product.id,
                "product_qty": line_data["qty"],       # purchase.order.line qty
                "product_uom_id": product.uom_id.id,   # Odoo 19: use standard UoM
                "name": line_data["desc"],
                "date_planned": fields.Datetime.now(),
            }
            if line_data.get("price") is not None:
                line_vals["price_unit"] = line_data["price"]

            self.env["purchase.order.line"].create(line_vals)

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
            "purchase_order_id": purchase_order.id,
            "matched_count": len(matched_lines),
            "unmatched_count": len(unmatched_items),
            "unmatched_text": unmatched_text,
            "state": "done",
        })

        _logger.info(
            "Created purchase order %s: %s matched, %s unmatched",
            purchase_order.name, len(matched_lines), len(unmatched_items)
        )

        return {
            "type": "ir.actions.act_window",
            "name": "Created Purchase Order",
            "res_model": "purchase.order",
            "res_id": purchase_order.id,
            "view_mode": "form",
            "target": "current",
        }
