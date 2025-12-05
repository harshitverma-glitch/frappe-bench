import frappe
import json
import re
from frappe.utils import add_days, getdate, now_datetime

@frappe.whitelist(allow_guest=False, methods=['POST'])
def receive_order():
    """
    Custom webhook endpoint for Etsy / Make.com
    Cleans incoming order JSON and creates/updates Sales Orders in ERPNext.
    """

    try:
        data = frappe.local.form_dict

        # Extract main fields
        transaction_id = data.get('transaction_id', '')
        order_data_raw = data.get('order_data', '')
        total_items = int(data.get('total_items', '1'))
        current_item = int(data.get('current_item', '1'))

        # Parse Etsy key-value pairs (e.g. "CUSTOMER: John Doe || PRODUCT: Table")
        parts = {}
        for part in order_data_raw.split("||"):
            if ":" in part:
                key, value = part.split(":", 1)
                parts[key.strip()] = value.strip()

        # Cleaning helper
        def clean_field(text):
            text = re.sub(r'[\r\n\t]+', ' ', text)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()

        # Extract cleaned fields
        customer_name = clean_field(parts.get("CUSTOMER", ""))
        receipt_id = clean_field(parts.get("RECEIPT", ""))
        transaction_id_parsed = clean_field(parts.get("TRANSACTION", transaction_id))
        transaction_date = clean_field(parts.get("DATE", ""))
        product_id = clean_field(parts.get("PRODUCT", ""))
        product_title = clean_field(parts.get("TITLE", ""))  # ← NEW: Extract title
        qty = clean_field(parts.get("QTY", "1"))
        rate = clean_field(parts.get("RATE", "0"))
        description = parts.get("DESC", "").strip()

        po_number = f"ETSY-{receipt_id}"

        # 1️⃣ Ensure Customer exists
        if not frappe.db.exists("Customer", customer_name):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_group": "All Customer Groups",
                "territory": "All Territories"
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        # 2️⃣ Ensure Item exists
        if not frappe.db.exists("Item", product_id):
            frappe.get_doc({
                "doctype": "Item",
                "item_code": product_id,
                "item_name": product_title,  # ← CHANGED: Use title instead of product_id
                "item_group": "Products",
                "is_sales_item": 1,
                "include_item_in_manufacturing": 0
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        # 3️⃣ Prepare custom properties (variations / personalization)
        var1_name = clean_field(parts.get("VAR1NAME", ""))
        var1_val = clean_field(parts.get("VAR1VAL", ""))
        var2_name = clean_field(parts.get("VAR2NAME", ""))
        var2_val = clean_field(parts.get("VAR2VAL", ""))
        var3_name = clean_field(parts.get("VAR3NAME", ""))
        var3_val = clean_field(parts.get("VAR3VAL", ""))

        if var1_name and var1_val:
            formatted_lines = []
            if var1_name and var1_val:
                formatted_lines.append(f"{var1_name}: {var1_val}")
            if var2_name and var2_val:
                formatted_lines.append(f"{var2_name}: {var2_val}")
            if var3_name and var3_val:
                formatted_lines.append(f"{var3_name}: {var3_val}")
            custom_properties = "\n".join(formatted_lines)
        else:
            # Clean up the description but keep newlines for readability
            desc = description.replace("Your Customization Summary", "").strip()
            lines = desc.split('\n')
            filtered_lines = [line for line in lines if not line.strip().startswith('Price')]
            desc = '\n'.join(filtered_lines)
            desc = re.sub(r'[\r\t]+', '', desc)
            desc = re.sub(r' +', ' ', desc)
            desc = re.sub(r'\n\n\n+', '\n\n', desc)
            custom_properties = desc.strip()

        # 4️⃣ Calculate dates
        try:
            trans_date = getdate(transaction_date)
        except:
            trans_date = now_datetime().date()
        delivery_date = add_days(trans_date, 7)
        ship_deadline = add_days(trans_date, 6)

        # 5️⃣ Check if Sales Order already exists
        existing_order = frappe.db.get_value("Sales Order", {"po_no": po_number}, "name")

        if existing_order:
            sales_order = frappe.get_doc("Sales Order", existing_order)

            # Skip if already submitted
            if sales_order.docstatus == 1:
                return {
                    'status': 'success',
                    'sales_order': sales_order.name,
                    'message': f'Sales Order {sales_order.name} already submitted'
                }

            # Add item to existing order (no duplicate check - each transaction is unique)
            sales_order.append("items", {
                "item_code": product_id,
                "delivery_date": delivery_date,
                "qty": float(qty),
                "rate": float(rate),
                "warehouse": "Finished Goods - CCP",
                "custom_shopify_properties": custom_properties
            })
            sales_order.save(ignore_permissions=True)
            frappe.db.commit()

            # If last item, submit the order
            if current_item >= total_items:
                sales_order.submit()
                frappe.db.commit()
                return {
                    'status': 'success',
                    'sales_order': sales_order.name,
                    'message': f"All {total_items} items added and Sales Order submitted.",
                    'submitted': True
                }

            return {
                'status': 'success',
                'sales_order': sales_order.name,
                'message': f"Item {current_item}/{total_items} added to existing Sales Order.",
                'item_added': True
            }

        # 6️⃣ Create new Sales Order
        sales_order = frappe.get_doc({
            "doctype": "Sales Order",
            "customer": customer_name,
            "transaction_date": trans_date,
            "delivery_date": delivery_date,
            "ship_deadline": ship_deadline,
            "company": "Cozy Corner Patios LLC",
            "order_type": "Sales",
            "po_no": po_number,
            "currency": "USD",
            "set_warehouse": "Finished Goods - CCP",
            "shopify_order_number": receipt_id,
            "items": [{
                "item_code": product_id,
                "delivery_date": delivery_date,
                "qty": float(qty),
                "rate": float(rate),
                "warehouse": "Finished Goods - CCP",
                "custom_shopify_properties": custom_properties
            }]
        })
        sales_order.insert(ignore_permissions=True)

        # Submit only if this is the last item
        if total_items == 1 or current_item >= total_items:
            sales_order.submit()

        frappe.db.commit()

        return {
            'status': 'success',
            'sales_order': sales_order.name,
            'message': f"Sales Order {sales_order.name} created successfully.",
            'submitted': current_item >= total_items
        }

    except Exception as e:
        frappe.log_error(f"Etsy Webhook Error: {str(e)}", "Etsy Webhook Failure")
        frappe.db.rollback()
        return {'status': 'error', 'message': str(e)}
