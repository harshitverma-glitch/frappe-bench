import frappe
import json
import re
from frappe.utils import add_days, getdate, now_datetime

@frappe.whitelist(allow_guest=False, methods=['POST'])
def receive_order():
    """
    Custom endpoint that handles newlines properly
    Receives Etsy order data from Make.com and creates Sales Order
    """

    try:
        # Get raw request data
        data = frappe.local.form_dict

        transaction_id = data.get('transaction_id', '')
        order_data_raw = data.get('order_data', '')

        # DON'T clean the raw data yet - we need to preserve structure
        # Parse the order data FIRST
        parts = {}
        for part in order_data_raw.split("||"):
            if ":" in part:
                key, value = part.split(":", 1)
                parts[key] = value.strip()

        # NOW clean individual fields (except DESC which needs newlines)
        def clean_field(text):
            text = re.sub(r'[\r\n\t]+', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()

        # Extract and clean data
        customer_name = clean_field(parts.get("CUSTOMER", ""))
        receipt_id = clean_field(parts.get("RECEIPT", ""))
        transaction_id_parsed = clean_field(parts.get("TRANSACTION", transaction_id))
        transaction_date = clean_field(parts.get("DATE", ""))
        product_id = clean_field(parts.get("PRODUCT", ""))
        qty = clean_field(parts.get("QTY", "1"))
        rate = clean_field(parts.get("RATE", "0"))

        # Get variations (clean them)
        var1_name = clean_field(parts.get("VAR1NAME", ""))
        var1_val = clean_field(parts.get("VAR1VAL", ""))
        var2_name = clean_field(parts.get("VAR2NAME", ""))
        var2_val = clean_field(parts.get("VAR2VAL", ""))
        var3_name = clean_field(parts.get("VAR3NAME", ""))
        var3_val = clean_field(parts.get("VAR3VAL", ""))

        # Get description - DON'T clean newlines!
        description = parts.get("DESC", "")

        # Check if variations exist
        has_variations = var1_name and var1_val

        # Initialize shopify_properties variable
        shopify_properties = ""

        # Format inline properties
        if has_variations:
            # Format with line breaks for variations
            formatted_lines = []
            if var1_name and var1_val:
                formatted_lines.append(f"{var1_name}: {var1_val}")
            if var2_name and var2_val:
                formatted_lines.append(f"{var2_name}: {var2_val}")
            if var3_name and var3_val:
                formatted_lines.append(f"{var3_name}: {var3_val}")

            shopify_properties = "\n".join(formatted_lines)
        else:
            # Keep newlines in description
            desc = description.strip()

            # Remove "Your Customization Summary" if it exists
            if desc.startswith("Your Customization Summary"):
                desc = desc.replace("Your Customization Summary", "", 1).strip()

            # Remove the Price line if present (starts with "Price")
            lines = desc.split('\n')
            filtered_lines = [line for line in lines if not line.strip().startswith('Price')]
            desc = '\n'.join(filtered_lines)

            # Only remove tabs and excessive spaces, keep newlines
            desc = re.sub(r'[\r\t]+', '', desc)
            desc = re.sub(r' +', ' ', desc)
            desc = re.sub(r'\n\n\n+', '\n\n', desc)

            shopify_properties = desc.strip()

        # Calculate delivery date
        try:
            trans_date = getdate(transaction_date)
            delivery_date = add_days(trans_date, 7)
        except:
            trans_date = now_datetime().date()
            delivery_date = add_days(trans_date, 7)

        # Create Sales Order directly
        sales_order = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": customer_name,

   "transaction_date": trans_date,
            "delivery_date": delivery_date,
            "company": "ABC_company",
            "order_type": "Sales",
            "po_no": f"ETSY-{receipt_id}-{transaction_id_parsed}",
            "currency": "USD",
            "custom_shopify_order_number": {receipt_id},

            "items": [{
                "item_code": product_id,
                "delivery_date": delivery_date,
                "qty": float(qty),
                "rate": float(rate),
                "custom_shopify_properties": shopify_properties
            }]
        })

        sales_order.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            'status': 'success',
            'sales_order': sales_order.name,
            'message': f'Sales Order {sales_order.name} created successfully',
            'shopify_properties': shopify_properties
        }

    except Exception as e:
        frappe.log_error(f"Etsy Webhook Error: {str(e)}", "Etsy Webhook")
        frappe.db.rollback()
        return {
            'status': 'error',
            'message': str(e)
        }
