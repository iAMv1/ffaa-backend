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


def _inventory_lines(inv, is_sales: bool) -> str:
    """ALLINVENTORYENTRIES per InvoiceItem. Fallback single stock if no items."""
    items = list(getattr(inv, "items", None) or [])
    if not items:
        desc = (inv.item_description or "Goods").strip() or "Goods"
        qty = float(inv.quantity or 1)
        taxable = float(inv.taxable_value or inv.total_amount or 0)
        rate = taxable / qty if qty else taxable
        items = [
            type(
                "X",
                (),
                {
                    "description": desc,
                    "quantity": qty,
                    "rate": rate,
                    "taxable_value": taxable,
                    "hsn_code": inv.hsn_code,
                    "gst_rate": inv.gst_rate or 0,
                },
            )()
        ]

    out = []
    for it in items:
        name = escape((it.description or "Item")[:100])
        qty = float(it.quantity or 1)
        rate = float(it.rate or 0)
        amt = float(it.taxable_value or (qty * rate))
        hsn = escape(str(it.hsn_code or inv.hsn_code or ""))
        # sales: stock out negative qty convention varies; keep amount + qty positive, type handles
        out.append(
            f"""
      <ALLINVENTORYENTRIES.LIST>
        <STOCKITEMNAME>{name}</STOCKITEMNAME>
        <ISDEEMEDPOSITIVE>{"No" if is_sales else "Yes"}</ISDEEMEDPOSITIVE>
        <RATE>{rate:.2f}/nos</RATE>
        <AMOUNT>{amt:.2f}</AMOUNT>
        <ACTUALQTY>{qty:.2f} nos</ACTUALQTY>
        <BILLEDQTY>{qty:.2f} nos</BILLEDQTY>
        <HSNCODE>{hsn}</HSNCODE>
      </ALLINVENTORYENTRIES.LIST>"""
        )
    return "".join(out)


def invoice_to_voucher_xml(inv) -> str:
    is_sales = (inv.invoice_type or "sales").lower() == "sales"
    vtype = "Sales" if is_sales else "Purchase"
    party = inv.company_name or "Party"
    amount = float(inv.total_amount or 0)
    taxable = float(inv.taxable_value or amount)
    cgst = float(inv.cgst or 0)
    sgst = float(inv.sgst or 0)
    igst = float(inv.igst or 0)
    inv_no = escape(inv.invoice_number or str(inv.id))
    inv_items = list(getattr(inv, "items", None) or [])
    use_inventory = bool(inv_items or inv.item_description)

    if is_sales:
        lines = _ledger(party, amount, True)
        if use_inventory:
            lines += _inventory_lines(inv, True)
        else:
            lines += _ledger("Sales", taxable, False)
        if cgst:
            lines += _ledger("CGST", cgst, False)
        if sgst:
            lines += _ledger("SGST", sgst, False)
        if igst:
            lines += _ledger("IGST", igst, False)
    else:
        if use_inventory:
            lines = _inventory_lines(inv, False)
        else:
            lines = _ledger("Purchase", taxable, True)
        if cgst:
            lines += _ledger("CGST", cgst, True)
        if sgst:
            lines += _ledger("SGST", sgst, True)
        if igst:
            lines += _ledger("IGST", igst, True)
        lines += _ledger(party, amount, False)

    return f"""
    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <VOUCHER VCHTYPE="{vtype}" ACTION="Create">
        <DATE>{_ymd(inv.invoice_date)}</DATE>
        <VOUCHERTYPENAME>{vtype}</VOUCHERTYPENAME>
        <VOUCHERNUMBER>{inv_no}</VOUCHERNUMBER>
        <NARRATION>Inv {inv_no}</NARRATION>
        <PARTYLEDGERNAME>{escape(party)}</PARTYLEDGERNAME>
        <ISINVOICE>Yes</ISINVOICE>{lines}
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
