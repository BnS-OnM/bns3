# PDF to Sales Quotation

## Description

This Odoo module allows you to import product information from PDF files and create sales quotations directly in Odoo without any intermediate XLSX export/import steps.

## Features

- **Upload PDF Files**: Simple wizard interface for uploading PDF files
- **Dual Format Support**: Automatic detection and parsing of 2 PDF formats:
  - **Bottom Bar Schema**: PDFs with "Appliances:" and "Controls:" sections
  - **Table Format**: Quotation PDFs with article numbers, quantities, descriptions, and prices
- **Smart Product Matching**: 
  - Primary matching by product code (default_code)
  - Fallback to fuzzy name matching (≥80% similarity)
  - Support for various code formats (VR, VRC, VWL, VIH, VP RW series)
- **Direct Sale Order Creation**: Creates sale.order with order lines directly from PDF
- **Flexible Pricing**: Option to use prices from PDF or let Odoo determine prices
- **Duplicate Handling**: Automatically combines quantities for duplicate products
- **Detailed Summary**: Shows matched and unmatched items with best candidates

## Installation

1. Install Python dependencies:
   ```bash
   pip install PyPDF2
   pip install rapidfuzz  # Optional, for better fuzzy matching
   ```

2. Install the module from Odoo Apps menu

3. The module depends on:
   - `sale` (Sales Management)
   - `product` (Product)

## Usage

### Accessing the Wizard

1. Navigate to **Sales > Orders > Importeer PDF naar offerte**
2. Or from the Quotations list view, use the action menu

### Using the Wizard

1. **Upload PDF File**: Select your PDF file
2. **Select Customer**: Choose the customer for this quotation (required)
3. **Use PDF Prices**: 
   - Enable to use prices from PDF (when available)
   - Disable to let Odoo determine prices from pricelist
4. **Reference Prefix**: Set a prefix for traceability (default: "GPT-001")
5. Click **"Maak offerte"** to create the quotation

### After Processing

The wizard will show:
- Link to the created sale order
- Number of matched product lines
- Number of unmatched items
- Details of unmatched items with best match candidates

## Supported PDF Formats

### Format 1: Bottom Bar Schema

PDFs containing sections like:
```
Appliances: VR71, VRC720, VWL 8.2 AS
Controls: VIH RW, VP RW 45/2 B
```

Product codes are extracted using regex patterns and matched against products in Odoo.

### Format 2: Table/Quotation Format

PDFs with product lines in table format:
```
956395 1 AROTHERM SPLIT PLUS ... 5.268,00 5.268,00
*208070 1 SCHROEFCILINDER M8 0,50 0,50
```

Format: `article_number quantity description [unit_price] [line_total]`

## Product Matching Logic

1. **Code Matching** (Exact):
   - Search in `product.product.default_code`
   - Fallback to `product.template.default_code`
   
2. **Fuzzy Name Matching** (≥80%):
   - Uses rapidfuzz if available, otherwise difflib
   - Searches product names with intelligent token filtering
   - Limited to top 100 candidates for performance

3. **Unmatched Items**:
   - Tracked with raw data and best candidate
   - Sale order is still created with matched items

## Supported Product Code Patterns

The module recognizes these code patterns:
- `VR\d{2,4}` (e.g., VR71, VR940)
- `VRC\s*\d{2,4}` (e.g., VRC720, VRC 720)
- `VWL\s*\d+(\.\d+)?\s*[A-Z]{1,4}` (e.g., VWL 8.2 AS)
- `VIH\s*[A-Z]{1,4}` (e.g., VIH RW)
- `VP\sRW\s\d+/\d+\s*[A-Z]` (e.g., VP RW 45/2 B)

## Limitations

- **No OCR Support**: The module requires text-based PDFs. Scanned PDFs without embedded text will be rejected.
- **No Automatic Partner Detection**: You must manually select the customer.
- **Format Detection**: Only supports the 2 predefined formats. Other formats will result in an error with debug information.
- **Price Format**: Only European number format (1.234,56) is supported for table format PDFs.

## Technical Details

- **Model**: `sale.pdf.to.quote.wizard` (TransientModel)
- **Dependencies**: PyPDF2 (required), rapidfuzz (optional but recommended)
- **Odoo Compatibility**: Designed for Odoo 16/17/19
- **Security**: Accessible by Sales > Salesman and Sales > Manager groups

## Troubleshooting

### "PDF bevat geen leesbare tekst"
The PDF is likely a scanned image. Use a text-based PDF instead.

### "Could not detect PDF format"
The PDF doesn't match either supported format. Check the error message for the first 30 lines of extracted text to debug.

### Poor Product Matching
- Ensure product codes in Odoo match the codes in the PDF
- Check that product names are similar enough for fuzzy matching (≥80%)
- Review unmatched items section for details

## License

LGPL-3

## Author

BenSo-tec
https://github.com/BnS-OnM/BenSo-tec
