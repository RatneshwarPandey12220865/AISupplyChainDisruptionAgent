"""Demo dataset: 6 suppliers across 5 regions, 3 US warehouses, 6 SKUs, in-transit shipments, customer
orders (some VIP), contract clauses and delivery history. Also defines the demo disruption scenarios."""
import random
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import drop_everything, init_db, session_scope
from app.models import (CustomerOrder, InventoryLevel, Product, Shipment, Supplier, SupplierDelivery,
                        SupplierProduct, Warehouse, utcnow)
from app.rag import add_contract

SUPPLIERS = [
    # name, country, region, port, lat, lon, reliability
    ("Shenzhen Precision Electronics", "China", "East Asia", "Port of Yantian", 22.57, 114.27, 0.86),
    ("Taipei Semicon Components", "Taiwan", "East Asia", "Port of Kaohsiung", 22.61, 120.28, 0.92),
    ("Monterrey Industrial Supply", "Mexico", "North America", "Monterrey rail hub", 25.69, -100.32, 0.88),
    ("Bavaria Components GmbH", "Germany", "Europe", "Port of Hamburg", 53.54, 9.97, 0.95),
    ("Hanoi Assembly Co", "Vietnam", "Southeast Asia", "Port of Hai Phong", 20.86, 106.68, 0.76),
    ("Chennai Electro Parts", "India", "South Asia", "Port of Chennai", 13.08, 80.29, 0.81),
]
WAREHOUSES = [("DAL", "Dallas DC", "North America"), ("NWK", "Newark DC", "North America"),
              ("RNO", "Reno DC", "North America")]
# sku, name, cost, price, demand/day/warehouse, std, safety, stockout penalty/unit, primary supplier idx
PRODUCTS = [
    ("MCU-32", "32-bit Microcontroller", 3.20, 7.50, 400, 80, 1200, 1.0, 1),
    ("LIC-18650", "Li-ion Cell 18650", 2.10, 4.80, 900, 150, 2700, 0.5, 5),
    ("PSU-65W", "65W Power Supply", 11.00, 24.00, 120, 25, 360, 3.0, 3),
    ("SNS-TMP", "Temperature Sensor", 0.90, 2.20, 1500, 300, 4500, 0.2, 2),
    ("HSG-AL", "Aluminium Housing", 6.50, 13.00, 200, 40, 600, 1.5, 4),
    ("CON-USBC", "USB-C Connector", 0.35, 0.90, 300, 60, 900, 0.1, 6),
]
# sku -> [(supplier idx, price, lead days, lead std, capacity/week, expedite?, expedite fee)]
SOURCING = {
    "MCU-32": [(1, 3.20, 21, 3, 20000, True, 0.15), (2, 3.65, 14, 1.5, 8000, False, 0), (5, 3.40, 18, 4, 6000, False, 0)],
    "LIC-18650": [(5, 2.10, 25, 5, 50000, True, 0.15), (1, 2.35, 21, 3, 20000, False, 0), (6, 2.25, 24, 4, 15000, False, 0)],
    "PSU-65W": [(3, 11.0, 12, 2, 3000, True, 0.12), (1, 9.8, 28, 4, 5000, False, 0), (4, 13.5, 9, 1, 1500, False, 0)],
    "SNS-TMP": [(2, 0.90, 18, 1.5, 80000, True, 0.10), (1, 0.95, 21, 3, 60000, False, 0), (6, 1.02, 20, 3, 30000, False, 0)],
    "HSG-AL": [(4, 6.5, 16, 1.5, 6000, True, 0.12), (3, 7.2, 8, 1.5, 3000, False, 0), (5, 5.9, 26, 5, 8000, False, 0)],
    "CON-USBC": [(6, 0.35, 10, 2, 20000, False, 0), (1, 0.33, 21, 3, 50000, False, 0), (3, 0.48, 5, 1, 8000, False, 0)],
}
INVENTORY = {  # sku -> DAL, NWK, RNO on-hand
    "MCU-32": (2800, 9000, 7000), "LIC-18650": (20000, 8100, 18000), "PSU-65W": (1500, 1600, 700),
    "SNS-TMP": (12000, 30000, 25000), "HSG-AL": (2500, 1800, 2400), "CON-USBC": (5000, 9000, 4800),
}
SHIPMENTS = [  # code, supplier idx, sku, warehouse, qty, eta days, mode, carrier, vessel, route region
    ("SHP-1001", 1, "MCU-32", "DAL", 12000, 6, "ocean", "COSCO", "COSCO Aries", "East Asia"),
    ("SHP-1002", 5, "LIC-18650", "NWK", 30000, 9, "ocean", "Maersk", "Maersk Hai Phong", "Southeast Asia"),
    ("SHP-1003", 3, "PSU-65W", "RNO", 2000, 4, "truck", "J.B. Hunt", "", "North America"),
    ("SHP-1004", 2, "SNS-TMP", "DAL", 40000, 7, "ocean", "Evergreen", "Ever Ace", "East Asia"),
    ("SHP-1005", 4, "HSG-AL", "NWK", 3000, 10, "ocean", "Hapag-Lloyd", "Berlin Express", "Europe"),
    ("SHP-1006", 1, "LIC-18650", "RNO", 15000, 12, "ocean", "COSCO", "COSCO Pisces", "East Asia"),
]
ORDERS = [  # number, customer, vip, sku, warehouse, qty, due days, penalty/day late
    ("ORD-5001", "Acme Robotics", True, "MCU-32", "DAL", 2000, 9, 1500),
    ("ORD-5002", "Northwind Devices", False, "MCU-32", "DAL", 1500, 12, 0),
    ("ORD-5003", "Globex Controls", False, "MCU-32", "DAL", 800, 5, 0),
    ("ORD-5010", "VoltEdge Mobility", True, "LIC-18650", "NWK", 6000, 11, 2500),
    ("ORD-5011", "Initech Power", False, "LIC-18650", "NWK", 3000, 14, 0),
    ("ORD-5012", "Cyberdyne Storage", False, "LIC-18650", "RNO", 5000, 16, 200),
    ("ORD-5020", "Stark Server Co", False, "PSU-65W", "RNO", 400, 7, 0),
    ("ORD-5021", "Umbrella Health", True, "PSU-65W", "RNO", 300, 9, 800),
    ("ORD-5030", "Hooli IoT", False, "SNS-TMP", "DAL", 5000, 10, 0),
    ("ORD-5040", "Wayne Devices", False, "HSG-AL", "NWK", 900, 12, 0),
    ("ORD-5050", "Soylent Gadgets", False, "CON-USBC", "DAL", 1200, 6, 0),
]
CONTRACTS = {
    1: [("MSA §7 Delivery performance & credits",
         "Supplier shall deliver within the confirmed lead time. For each day of delay beyond 3 days, Buyer receives a "
         "credit of 0.5% of the affected line value, capped at 10%. Supplier offers priority production and premium "
         "air freight at 15% surcharge on request, confirmed within 12 hours."),
        ("MSA §14 Force majeure",
         "Port congestion, typhoons and government export controls constitute force majeure; late-delivery credits are "
         "suspended during force majeure. Supplier must notify within 48 hours and propose a recovery schedule.")],
    2: [("Supply Agreement §5 Lead times",
         "Standard lead time 18 days; express lane 14 days for orders placed before 10:00 Taipei time. Late delivery "
         "credit 1% per day up to 8%. Buyer may reallocate up to 30% of forecast volume without penalty."),
        ("Supply Agreement §9 Allocation",
         "During component shortages, Supplier allocates capacity pro-rata to trailing 6-month volume. "
         "Buyer holds a Gold allocation tier.")],
    3: [("Contract §4 Delivery & expedite",
         "Truck delivery to US DCs within 12 days; expedited team-driver service available at 12% fee. Liquidated "
         "damages of $150 per day for late delivery of priority orders."),
        ("Contract §11 Capacity",
         "Supplier maintains safety stock of 2 weeks of forecast for Buyer. Capacity increases require 3 weeks notice.")],
    4: [("Rahmenvertrag §6 Delivery",
         "Delivery DAP US port within 16 days; air freight option within 9 days at Buyer's cost. Supplier has "
         "IATF 16949 certification. Penalty 0.3% per day late, max 5%."),
        ("Rahmenvertrag §12 Minimum order",
         "Minimum order quantity waived for orders above EUR 5,000.")],
    5: [("Supply Contract §3 Delivery terms",
         "FOB Hai Phong, 25-day lead time. No late-delivery credits. Expedite by split shipment at 15% surcharge."),
        ("Supply Contract §10 Force majeure",
         "Typhoon season (Jul-Nov) disruptions are force majeure. Supplier may reduce allocations by up to 50% during "
         "force majeure events.")],
    6: [("Purchase Agreement §5 Delivery",
         "CIF US port within 24 days for cells, 10 days for connectors via air consolidation. Late credit 0.5% per day."),
        ("Purchase Agreement §8 Quality",
         "Batches failing incoming inspection are replaced within 10 days at Supplier's cost.")],
}

SCENARIOS = {
    "port_congestion": ("external.signal", {
        "kind": "port_congestion", "region": "East Asia", "severity": 0.75, "duration_hours": 120,
        "description": "Yantian and Kaohsiung terminals at 92% yard utilisation; vessel waiting times 5-7 days."}),
    "shipment_delay": ("shipment.eta_update", {
        "shipment_code": "SHP-1002", "delay_days": 10,
        "reason": "Vessel omitted Hai Phong call; cargo rolled to next sailing via Singapore."}),
    "supplier_lead_time": ("supplier.update", {
        "supplier_id": 3, "sku": "PSU-65W", "lead_time_days": 26,
        "message": "Electrolytic capacitor shortage at Monterrey plant."}),
    "low_stock": ("inventory.update", {"sku": "CON-USBC", "warehouse": "DAL", "on_hand": 1200}),
    "cargo_temperature": ("telemetry.reading", {"shipment_code": "SHP-1006", "metric": "temperature_c",
                                                "value": 47.5}),
    "supplier_outage": ("supplier.update", {
        "supplier_id": 5, "status": "force_majeure", "message": "Typhoon damage at Hai Phong facility."}),
}


def seed(session: Session) -> bool:
    if session.scalar(select(Supplier.id).limit(1)):
        return False
    rng = random.Random(42)
    now = utcnow()
    suppliers = []
    for name, country, region, port, lat, lon, rel in SUPPLIERS:
        s = Supplier(name=name, country=country, region=region, port=port, lat=lat, lon=lon, reliability_score=rel,
                     contact_email=f"planning@{name.split()[0].lower()}.example.com")
        session.add(s)
        suppliers.append(s)
    whs = {code: Warehouse(code=code, name=name, region=region) for code, name, region in WAREHOUSES}
    session.add_all(whs.values())
    session.flush()

    products = {}
    for sku, name, cost, price, demand, std, safety, penalty, sup_idx in PRODUCTS:
        primary_lead = next(x[2] for x in SOURCING[sku] if x[0] == sup_idx)
        p = Product(sku=sku, name=name, unit_cost=cost, unit_price=price, daily_demand_mean=demand,
                    daily_demand_std=std, safety_stock=safety, reorder_point=demand * primary_lead + safety,
                    stockout_penalty_per_unit=penalty, primary_supplier_id=suppliers[sup_idx - 1].id)
        session.add(p)
        products[sku] = p
    session.flush()

    for sku, rows in SOURCING.items():
        for sup_idx, price, lead, lstd, cap, exp, fee in rows:
            sup = suppliers[sup_idx - 1]
            session.add(SupplierProduct(supplier_id=sup.id, product_id=products[sku].id, unit_price=price,
                                        lead_time_days=lead, lead_time_std_days=lstd, capacity_per_week=cap,
                                        min_order_qty=500, expedite_available=exp, expedite_fee_pct=fee))
            for i in range(12):  # delivery history: late more often for less reliable suppliers
                slip = max(-1.0, rng.gauss((1 - sup.reliability_score) * lead * 0.4, lstd))
                session.add(SupplierDelivery(supplier_id=sup.id, product_id=products[sku].id, promised_days=lead,
                                             actual_days=round(lead + slip, 1),
                                             delivered_at=now - timedelta(days=30 * (i + 1))))

    for sku, levels in INVENTORY.items():
        for code, qty in zip(("DAL", "NWK", "RNO"), levels):
            session.add(InventoryLevel(product_id=products[sku].id, warehouse_id=whs[code].id, on_hand=qty))

    for code, sup_idx, sku, wh, qty, eta, mode, carrier, vessel, route in SHIPMENTS:
        session.add(Shipment(code=code, supplier_id=suppliers[sup_idx - 1].id, product_id=products[sku].id,
                             warehouse_id=whs[wh].id, quantity=qty, original_eta=now + timedelta(days=eta),
                             eta=now + timedelta(days=eta), mode=mode, carrier=carrier, vessel=vessel,
                             route_region=route))

    for number, customer, vip, sku, wh, qty, due, penalty in ORDERS:
        session.add(CustomerOrder(order_number=number, customer=customer, is_vip=vip, product_id=products[sku].id,
                                  warehouse_id=whs[wh].id, quantity=qty, unit_price=products[sku].unit_price,
                                  due_date=now + timedelta(days=due), penalty_per_day_late=penalty))

    for sup_idx, docs in CONTRACTS.items():
        for title, content in docs:
            add_contract(session, suppliers[sup_idx - 1].id, title, content)
    return True


def reset_database() -> None:
    drop_everything()
    init_db()
    with session_scope() as s:
        seed(s)


if __name__ == "__main__":
    init_db()
    with session_scope() as s:
        print("Seeded demo data" if seed(s) else "Database already seeded")
