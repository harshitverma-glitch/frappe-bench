import frappe
import json
import re
from frappe.utils import add_days, getdate, now_datetime


# ===========================================================================
# Webhook authentication helper
# ===========================================================================

def _verify_webhook_secret():
    """
    Validate the X-Webhook-Secret header against the value stored in
    site_config.json under the key `etsy_webhook_secret`.
    Raises frappe.PermissionError (HTTP 403) if missing or wrong.
    """
    expected = frappe.conf.get("etsy_webhook_secret")
    if not expected:
        return
        

    received = frappe.get_request_header("X-Webhook-Secret") or ""
    if received != expected:
        frappe.log_error(
            "Invalid or missing X-Webhook-Secret header on Etsy webhook call",
            "Etsy Webhook Auth",
        )
        frappe.throw(
            "Invalid webhook secret.",
            frappe.PermissionError,
        )


# ===========================================================================
# Receive order from Etsy via Make.com
# ===========================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
def receive_order():
    """
    Custom webhook endpoint for Etsy / Make.com
    Cleans incoming order JSON and creates/updates Sales Orders in ERPNext.
    """

    _verify_webhook_secret()

    try:
        data = frappe.local.form_dict

        # Extract main fields
        transaction_id = data.get('transaction_id', '')
        order_data_raw = data.get('order_data', '')
        total_items = int(data.get('total_items', '1'))
        current_item = int(data.get('current_item', '1'))
        sales_channel = data.get('sales_channel', '')
        etsy_net_total_raw = str(data.get('etsy_net_total', '0'))
        if '/' in etsy_net_total_raw:
            num, div = etsy_net_total_raw.split('/')
            etsy_net_total = float(num.strip()) / float(div.strip())
        else:
            etsy_net_total = float(etsy_net_total_raw or 0)
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
        product_title = clean_field(parts.get("TITLE", ""))  # NEW: Extract title
        qty = clean_field(parts.get("QTY", "1"))
        rate = clean_field(parts.get("RATE", "0"))
        description = parts.get("DESC", "").strip()

        po_number = f"ETSY-{receipt_id}"

        # 1. Ensure Customer exists
        if not frappe.db.exists("Customer", customer_name):
            frappe.get_doc({
                "doctype": "Customer",
                "customer_name": customer_name,
                "customer_group": "All Customer Groups",
                "territory": "All Territories"
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        # 2. Ensure Item exists
        if not frappe.db.exists("Item", product_id):
            frappe.get_doc({
                "doctype": "Item",
                "item_code": product_id,
                "item_name": product_title,  # CHANGED: Use title instead of product_id
                "item_group": "Products",
                "is_sales_item": 1,
                "include_item_in_manufacturing": 0
            }).insert(ignore_permissions=True)
            frappe.db.commit()

        # 3. Prepare custom properties (variations / personalization)
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

        # 4. Calculate dates
        try:
            trans_date = getdate(transaction_date)
        except:
            trans_date = now_datetime().date()
        delivery_date = add_days(trans_date, 7)
        ship_deadline = add_days(trans_date, 6)

        # 5. Check if Sales Order already exists
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

        # 6. Create new Sales Order
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
            "custom_sales_channel": sales_channel,  # NEW: Save the sales channel (Etsy Maria or Etsy Zipcushions)
            "custom_etsy_net_total": etsy_net_total,
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

    except frappe.PermissionError:
        # Re-raise auth errors so Frappe returns a real 403 to Make
        raise
    except Exception as e:
        frappe.log_error(f"Etsy Webhook Error: {str(e)}", "Etsy Webhook Failure")
        frappe.db.rollback()
        return {'status': 'error', 'message': str(e)}


# =============================================================================
# UPDATE ADDRESS FUNCTION - WITH EMAIL SUPPORT
# =============================================================================

@frappe.whitelist(allow_guest=True, methods=['POST'])
def update_address():
    """
    Update Sales Order with shipping address from Gmail parsing.
    Called by Make.com scenario that parses Etsy sale notification emails.

    Expected parameters:
    - order_id: Etsy order/receipt number (e.g., "3938139725")
    - recipient_name: Customer name from shipping address
    - address_line1: Street address
    - address_line2: (optional) Apartment, suite, etc.
    - city: City name
    - state: State/province code
    - zip: Postal/ZIP code
    - country: Country name
    - email_id: (NEW) Buyer's email address
    - phone: (NEW) Buyer's phone number (optional)
    """

    _verify_webhook_secret()

    try:
        data = frappe.local.form_dict

        # Extract parameters
        order_id = data.get('order_id', '').strip()
        recipient_name = data.get('recipient_name', '').strip()
        address_line1 = data.get('address_line1', '').strip()
        address_line2 = data.get('address_line2', '').strip()
        city = data.get('city', '').strip()
        state = data.get('state', '').strip()
        zip_code = data.get('zip', '').strip()
        country = data.get('country', '').strip()
        email_id = data.get('email_id', '').strip()  # NEW: Extract email
        phone = data.get('phone', '').strip()  # NEW: Extract phone (for future use)

        # Validate required fields
        if not order_id:
            return {
                'status': 'error',
                'message': 'Missing order_id parameter'
            }

        if not address_line1 or not city or not state or not zip_code:
            return {
                'status': 'error',
                'message': 'Missing required address fields (address_line1, city, state, zip)'
            }

        # Find the Sales Order by PO Number (ETSY-{receipt_id})
        po_number = f"ETSY-{order_id}"
        sales_order_name = frappe.db.get_value("Sales Order", {"po_no": po_number}, "name")

        if not sales_order_name:
            return {
                'status': 'error',
                'message': f'Sales Order with PO# {po_number} not found'
            }

        # Get the Sales Order
        sales_order = frappe.get_doc("Sales Order", sales_order_name)
        customer_name = sales_order.customer

        # Format the full address for display
        address_parts = [address_line1]
        if address_line2:
            address_parts.append(address_line2)
        address_parts.append(f"{city}, {state} {zip_code}")
        if country:
            address_parts.append(country)
        full_address = "\n".join(address_parts)

        # Create or update Address in ERPNext
        address_title = f"{recipient_name} - {order_id}"

        # Check if address already exists
        existing_address = frappe.db.get_value("Address", {"address_title": address_title}, "name")

        if existing_address:
            # Update existing address
            address_doc = frappe.get_doc("Address", existing_address)
            address_doc.address_line1 = address_line1
            address_doc.address_line2 = address_line2
            address_doc.city = city
            address_doc.state = state
            address_doc.pincode = zip_code
            address_doc.country = country if country else "United States"
            # NEW: Update email if provided
            if email_id:
                address_doc.email_id = email_id
            # NEW: Update phone if provided
            if phone:
                address_doc.phone = phone
            address_doc.save(ignore_permissions=True)
        else:
            # Create new address
            address_doc = frappe.get_doc({
                "doctype": "Address",
                "address_title": address_title,
                "address_type": "Shipping",
                "address_line1": address_line1,
                "address_line2": address_line2,
                "city": city,
                "state": state,
                "pincode": zip_code,
                "country": country if country else "United States",
                "email_id": email_id if email_id else "",  # NEW: Add email
                "phone": phone if phone else "",  # NEW: Add phone
                "links": [{
                    "link_doctype": "Customer",
                    "link_name": customer_name
                }]
            })
            address_doc.insert(ignore_permissions=True)

        frappe.db.commit()

        # Update Sales Order with shipping address
        # Use db_set to update even submitted documents
        frappe.db.set_value("Sales Order", sales_order_name, {
            "shipping_address_name": address_doc.name,
            "shipping_address": full_address
        }, update_modified=False)
        frappe.db.commit()

        return {
            'status': 'success',
            'message': f'Address updated for Sales Order {sales_order_name}',
            'sales_order': sales_order_name,
            'address': address_doc.name,
            'full_address': full_address,
            'email_id': email_id,  # NEW: Return email in response
            'phone': phone,  # NEW: Return phone in response
            'docstatus': sales_order.docstatus
        }

    except frappe.PermissionError:
        raise
    except Exception as e:
        frappe.log_error(f"Update Address Error: {str(e)}", "Etsy Address Webhook")
        frappe.db.rollback()
        return {
            'status': 'error',
            'message': str(e)
        }
