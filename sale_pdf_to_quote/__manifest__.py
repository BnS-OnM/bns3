{
    'name': 'PDF to Sales Quotation',
    'version': '19.0.1.0.0',
    'category': 'Sales',
    'summary': 'Import PDF files and create sales quotations or purchase orders directly',
    'description': """
        PDF to Sales Quotation + Purchase Order
        ======================================
        - Sales: import PDF -> sales quotation
        - Purchase: import PDF -> purchase order

        Technical:
        - Uses PyPDF2 for PDF text extraction
        - Uses rapidfuzz or difflib for fuzzy matching
        - Supports Odoo 19
    """,
    'author': 'BenSo-tec',
    'website': 'https://github.com/BnS-OnM/BenSo-tec',
    'depends': ['sale', 'product', 'purchase'],
    'external_dependencies': {
        'python': ['PyPDF2'],
    },
    'data': [
        'security/ir.model.access.csv',

        # Sales wizard (bestaat al bij jou)
        'views/pdf_to_quote_wizard_views.xml',
        'views/sale_pdf_menu.xml',

        # Purchase wizard (nieuw)
        'views/purchase_pdf_to_order_wizard_views.xml',
        'views/purchase_pdf_menu.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
    'license': 'LGPL-3',
}
