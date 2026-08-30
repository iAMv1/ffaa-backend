"""Tally XML. ponytail: string template; multi-line inventory when InvoiceItem rows exist."""
from datetime import date
from xml.sax.saxutils import escape


def _ymd(d: date | None) -> str:
    return d.strftime("%Y%m%d") if d else ""


def _ledger(name: str, amount: float, deemed_positive: bool) -> str:
    sign = "-" if deemed_positive else ""
    yes = "Yes" if deemed_positive else "No"
    return f"""
      <ALLLEDGERENTRIES.LIST>
        <LEDGERNAME>{escape(name)}</LEDGERNAME>
        <ISDEEMEDPOSITIVE>{yes}</ISDEEMEDPOSITIVE>
        <AMOUNT>{sign}{abs(amount):.2f}</AMOUNT>
      </ALLLEDGERENTRIES.LIST>"""


def _fallback_stock_item(inv) -> object:
    """Single stock row when an invoice has no line items (matches prior dynamic type)."""
    qty = float(inv.quantity or 1)
    taxable = float(inv.taxable_value or inv.total_amount or 0)
    rate = taxable / qty if qty else taxable
    return type(
        "X",
        (),
        {
            "description": (inv.item_description or "Goods").strip() or "Goods",
            "quantity": qty,
            "rate": rate,
            "taxable_value": taxable,
            "hsn_code": inv.hsn_code,
            "gst_rate": inv.gst_rate or 0,
        },
    )()


def _inventory_entry(it, is_sales: bool, inv_hsn) -> str:
    name = escape((it.description or "Item")[:100])
    qty = float(it.quantity or 1)
    rate = float(it.rate or 0)
    amt = float(it.taxable_value or (qty * rate))
    hsn = escape(str(it.hsn_code or inv_hsn or ""))
    return f"""
      <ALLINVENTORYENTRIES.LIST>
        <STOCKITEMNAME>{name}</STOCKITEMNAME>
        <ISDEEMEDPOSITIVE>{"No" if is_sales else "Yes"}</ISDEEMEDPOSITIVE>
        <RATE>{rate:.2f}/nos</RATE>
        <AMOUNT>{amt:.2f}</AMOUNT>
        <ACTUALQTY>{qty:.2f} nos</ACTUALQTY>
        <BILLEDQTY>{qty:.2f} nos</BILLEDQTY>
        <HSNCODE>{hsn}</HSNCODE>
      </ALLINVENTORYENTRIES.LIST>"""


def _inventory_lines(inv, is_sales: bool) -> str:
    """ALLINVENTORYENTRIES per InvoiceItem. Fallback single stock if no items."""
    items = list(getattr(inv, "items", None) or [])
    if not items:
        items = [_fallback_stock_item(inv)]
    return "".join(_inventory_entry(it, is_sales, inv.hsn_code) for it in items)


def _tax_ledgers(cgst: float, sgst: float, igst: float, tax_positive: bool) -> str:
    lines = ""
    if cgst:
        lines += _ledger("CGST", cgst, tax_positive)
    if sgst:
        lines += _ledger("SGST", sgst, tax_positive)
    if igst:
        lines += _ledger("IGST", igst, tax_positive)
    return lines


def invoice_to_voucher_xml(inv) -> str:
    is_sales = (inv.invoice_type or "sales").lower() == "sales"
    party = inv.company_name or "Party"
    amount = float(inv.total_amount or 0)
    taxable = float(inv.taxable_value or amount)
    cgst = float(inv.cgst or 0)
    sgst = float(inv.sgst or 0)
    igst = float(inv.igst or 0)
    inv_no = escape(inv.invoice_number or str(inv.id))
    inv_items = list(getattr(inv, "items", None) or [])
    use_inventory = bool(inv_items or inv.item_description)
    vtype = "Sales" if is_sales else "Purchase"

    body = _ledger(party, amount, True) if is_sales else ""
    body += _inventory_lines(inv, is_sales) if use_inventory else _ledger(vtype, taxable, not is_sales)
    body += _tax_ledgers(cgst, sgst, igst, not is_sales)
    if not is_sales:
        body += _ledger(party, amount, False)

    return f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <VOUCHER VCHTYPE="{vtype}" ACTION="Create">
        <DATE>{_ymd(inv.invoice_date)}</DATE>
        <VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
        <VOUCHERNUMBER>{inv_no}</VOUCHERNUMBER>
        <NARRATION>Inv {inv_no}</NARRATION>
        <PARTYLEDGERNAME>{escape(party)}</PARTYLEDGERNAME>
        <ISINVOICE>Yes</ISINVOICE>{body}
      </VOUCHER>
    </TALLYMESSAGE>"""


def invoices_to_tally_xml(invoices: list) -> str:
    body = "".join(invoice_to_voucher_xml(i) for i in invoices)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>Vouchers</REPORTNAME>
      </REQUESTDESC>
      <REQUESTDATA>{body}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>
"""
