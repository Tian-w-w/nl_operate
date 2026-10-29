"""Independent, versioned branch fixture. Never changes the user's demo DB."""
import json
from datetime import datetime, timedelta
from decimal import Decimal

from backend.app.config import ROOT
from backend.app.models import Customer, Order, OrderItem, Product, Shipment

FIXTURE_PATH = ROOT / 'data/fixtures/agent_branch_v1.json'


def load_fixture(db):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    reference = datetime.fromisoformat(fixture['reference_time'])
    for ident, tenant in [(1, 'a'), (2, 'b')]:
        db.add(Customer(id=ident, tenant_id=tenant, name=f'分支测试客户{tenant}'))
        db.add(Product(id=ident, tenant_id=tenant, name=f'分支测试商品{tenant}', category='办公'))
    db.flush()
    for ident, tenant, city, status, amount, age in fixture['rows']:
        owner = 1 if tenant == 'a' else 2
        paid = reference - timedelta(hours=age)
        shipped = paid + timedelta(hours=1) if status == 'shipped' else None
        db.add(Order(id=ident, tenant_id=tenant, customer_id=owner, shipping_city=city, status=status,
                     total_amount=Decimal(amount), created_at=reference-timedelta(days=10), paid_at=paid,
                     shipped_at=shipped, priority=False, review_status='normal', version=1,
                     note='合成分支测试数据'))
        db.flush()
        db.add(OrderItem(tenant_id=tenant, order_id=ident, product_id=owner, quantity=1, unit_price=Decimal(amount)))
        if shipped:
            db.add(Shipment(tenant_id=tenant, order_id=ident, carrier='测试物流', tracking_no=f'TEST{ident}', shipped_at=shipped))
    return fixture
